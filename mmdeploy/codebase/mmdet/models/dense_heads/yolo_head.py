# Copyright (c) OpenMMLab. All rights reserved.
from typing import Sequence

import numpy as np
import torch
from mmdet.utils.typing import OptConfigType
from torch import Tensor

from mmdeploy.codebase.mmdet import (get_post_processing_params,
                                     pad_with_value_if_necessary)
from mmdeploy.codebase.mmdet.models.layers import multiclass_nms
from mmdeploy.core import FUNCTION_REWRITER, mark
from mmdeploy.utils import Backend, is_dynamic_shape


@FUNCTION_REWRITER.register_rewriter(
    func_name='mmdet.models.dense_heads.yolo_head.'
    'YOLOV3Head.predict_by_feat')
def yolov3_head__predict_by_feat(ctx,
                                 self,
                                 pred_maps: Sequence[Tensor],
                                 cfg: OptConfigType = None,
                                 rescale: bool = False,
                                 with_nms: bool = True,
                                 **kwargs):
    """Rewrite `predict_by_feat` of `YOLOV3Head` for default backend.

    Rewrite this function to deploy model, transform network output for a
    batch into bbox predictions.

    Args:
        ctx (ContextCaller): The context with additional information.
        pred_maps (Sequence[Tensor]): Raw predictions for a batch of
            images.
        cfg (ConfigDict, optional): Test / postprocessing
            configuration, if None, test_cfg would be used.
            Defaults to None.
        rescale (bool): If True, return boxes in original image space.
            Defaults to False.
        with_nms (bool): If True, do nms before return boxes.
            Defaults to True.

    Returns:
        If with_nms == True:
            tuple[Tensor, Tensor]: tuple[Tensor, Tensor]: (dets, labels),
            `dets` of shape [N, num_det, 5] and `labels` of shape
            [N, num_det].
        Else:
            tuple[Tensor, Tensor]: batch_mlvl_bboxes, batch_mlvl_scores
    """
    deploy_cfg = ctx.cfg

    # mark pred_maps
    @mark('yolo_head', inputs=['pred_maps'])
    def __mark_pred_maps(pred_maps):
        return pred_maps

    pred_maps = __mark_pred_maps(pred_maps)

    is_dynamic_flag = is_dynamic_shape(ctx.cfg)
    num_levels = len(pred_maps)
    pred_maps_list = [pred_maps[i].detach() for i in range(num_levels)]

    cfg = self.test_cfg if cfg is None else cfg
    assert len(pred_maps_list) == self.num_levels

    device = pred_maps_list[0].device
    batch_size = pred_maps_list[0].shape[0]

    featmap_sizes = [
        pred_maps_list[i].shape[-2:] for i in range(self.num_levels)
    ]
    multi_lvl_anchors = self.prior_generator.grid_anchors(
        featmap_sizes, device)
    pre_topk = cfg.get('nms_pre', -1)
    multi_lvl_bboxes = []
    multi_lvl_cls_scores = []
    multi_lvl_conf_scores = []
    for i in range(self.num_levels):
        # get some key info for current scale
        pred_map = pred_maps_list[i]
        stride = self.featmap_strides[i]
        # (b,h, w, num_anchors*num_attrib) ->
        # (b,h*w*num_anchors, num_attrib)
        pred_map = pred_map.permute(0, 2, 3,
                                    1).reshape(batch_size, -1, self.num_attrib)
        # Inplace operation like
        # ```pred_map[..., :2] = \torch.sigmoid(pred_map[..., :2])```
        # would create constant tensor when exporting to onnx
        pred_map_conf = torch.sigmoid(pred_map[..., :2])
        pred_map_rest = pred_map[..., 2:]
        pred_map = torch.cat([pred_map_conf, pred_map_rest], dim=-1)
        pred_map_boxes = pred_map[..., :4]
        multi_lvl_anchor = multi_lvl_anchors[i]
        # use static anchor if input shape is static
        if not is_dynamic_flag:
            multi_lvl_anchor = multi_lvl_anchor.data
        multi_lvl_anchor = multi_lvl_anchor.unsqueeze(0)
        bbox_pred = self.bbox_coder.decode(multi_lvl_anchor, pred_map_boxes,
                                           stride)
        # conf and cls
        conf_pred = torch.sigmoid(pred_map[..., 4])
        cls_pred = torch.sigmoid(pred_map[..., 5:]).view(
            batch_size, -1, self.num_classes)  # Cls pred one-hot.

        # Save the result of current scale
        multi_lvl_bboxes.append(bbox_pred)
        multi_lvl_cls_scores.append(cls_pred)
        multi_lvl_conf_scores.append(conf_pred)
    # Merge the results of different scales together
    batch_mlvl_bboxes = torch.cat(multi_lvl_bboxes, dim=1)
    batch_mlvl_scores = torch.cat(multi_lvl_cls_scores, dim=1)
    batch_mlvl_conf_scores = torch.cat(multi_lvl_conf_scores, dim=1)

    post_params = get_post_processing_params(deploy_cfg)
    score_threshold = cfg.get('score_thr', post_params.score_threshold)
    confidence_threshold = cfg.get('conf_thr',
                                   post_params.confidence_threshold)

    # follow original pipeline of YOLOv3
    if confidence_threshold > 0:
        mask = batch_mlvl_conf_scores >= confidence_threshold
        batch_mlvl_conf_scores = batch_mlvl_conf_scores.where(
            mask, batch_mlvl_conf_scores.new_zeros(1))
        batch_mlvl_scores = batch_mlvl_scores.where(
            mask.unsqueeze(-1), batch_mlvl_scores.new_zeros(1))
        batch_mlvl_bboxes = batch_mlvl_bboxes.where(
            mask.unsqueeze(-1), batch_mlvl_bboxes.new_zeros(1))

    if score_threshold > 0:
        mask = batch_mlvl_scores > score_threshold
        batch_mlvl_scores = batch_mlvl_scores.where(
            mask, batch_mlvl_scores.new_zeros(1))

    if pre_topk > 0:
        batch_mlvl_bboxes = pad_with_value_if_necessary(
            batch_mlvl_bboxes, 1, pre_topk)
        batch_mlvl_conf_scores = pad_with_value_if_necessary(
            batch_mlvl_conf_scores, 1, pre_topk, 0.)
        batch_mlvl_scores = pad_with_value_if_necessary(
            batch_mlvl_scores, 1, pre_topk, 0.)
        _, topk_inds = batch_mlvl_scores.reshape(batch_size, -1). \
            topk(pre_topk, dim=1)
        topk_inds = torch.stack(
            [topk_inds // self.num_classes, topk_inds % self.num_classes])
        batch_inds = torch.arange(
            batch_size, device=device).unsqueeze(-1).long()
        # Avoid onnx2tensorrt issue in https://github.com/NVIDIA/TensorRT/issues/1134 # noqa: E501
        transformed_inds = (
            batch_mlvl_bboxes.shape[1] * batch_inds + topk_inds)
        batch_mlvl_bboxes = batch_mlvl_bboxes. \
            reshape(-1, 4)[transformed_inds, :].reshape(batch_size, -1, 4)
        batch_mlvl_scores = batch_mlvl_scores.reshape(
            -1, self.num_classes)[transformed_inds, :].reshape(
                batch_size, -1, self.num_classes)
        batch_mlvl_conf_scores = batch_mlvl_conf_scores. \
            reshape(-1, 1)[transformed_inds].reshape(batch_size, -1)

    batch_mlvl_conf_scores = batch_mlvl_conf_scores.unsqueeze(2)
    batch_mlvl_scores = batch_mlvl_scores * batch_mlvl_conf_scores
    if with_nms:
        max_output_boxes_per_class = post_params.max_output_boxes_per_class
        iou_threshold = cfg.nms.get('iou_threshold', post_params.iou_threshold)
        pre_top_k = post_params.pre_top_k
        keep_top_k = cfg.get('max_per_img', post_params.keep_top_k)
        # keep aligned with original pipeline, improve
        # mAP by 1% for YOLOv3 in ONNX
        score_threshold = 0
        return multiclass_nms(
            batch_mlvl_bboxes,
            batch_mlvl_scores,
            max_output_boxes_per_class,
            iou_threshold=iou_threshold,
            score_threshold=score_threshold,
            pre_top_k=pre_top_k,
            keep_top_k=keep_top_k)
    else:
        return batch_mlvl_bboxes, batch_mlvl_scores


@FUNCTION_REWRITER.register_rewriter(
    func_name='mmdet.models.dense_heads.YOLOV3Head.predict_by_feat',
    backend=Backend.NCNN.value)
def yolov3_head__predict_by_feat__ncnn(ctx,
                                       self,
                                       pred_maps,
                                       with_nms=True,
                                       cfg=None,
                                       **kwargs):
    """Rewrite `predict_by_feat` of YOLOV3Head for ncnn backend.

    1. Shape node and batch inference is not supported by ncnn. This function
    transform dynamic shape to constant shape and remove batch inference.
    2. Batch dimension is not supported by ncnn, but supported by pytorch.
    The negative value of axis in torch.cat is rewritten as corresponding
    positive value to avoid axis shift.
    3. 2-dimension tensor broadcast of `BinaryOps` operator is not supported by
    ncnn. This function unsqueeze 2-dimension tensor to 3-dimension tensor for
    correct `BinaryOps` calculation by ncnn.


    Args:
        ctx (ContextCaller): The context with additional information.

        self: Represent the instance of the original class.
        pred_maps (list[Tensor]): Raw predictions for a batch of images.
        with_nms (bool): If True, do nms before return boxes.
            Default: True.
        cfg (mmcv.Config | None): Test / postprocessing configuration,
            if None, test_cfg would be used. Default: None.

    Returns:
            Tensor: Detection_output of shape [num_boxes, 6],
            each row is [label, score, x1, y1, x2, y2]. Note that
            fore-ground class label in Yolov3DetectionOutput starts
            from `1`. x1, y1, x2, y2 are normalized in range(0,1).
    """
    num_levels = len(pred_maps)
    cfg = self.test_cfg if cfg is None else cfg
    post_params = get_post_processing_params(ctx.cfg)

    confidence_threshold = cfg.get('conf_thr',
                                   post_params.confidence_threshold)
    iou_threshold = cfg.nms.get('iou_threshold', post_params.iou_threshold)
    anchor_biases = np.array(
        self.prior_generator.base_sizes).reshape(-1).tolist()
    num_box = len(self.prior_generator.base_sizes[0])
    bias_masks = list(range(num_levels * num_box))

    def _create_yolov3_detection_output():
        """Help create Yolov3DetectionOutput op in ONNX."""

        class Yolov3DetectionOutputOp(torch.autograd.Function):
            """Create Yolov3DetectionOutput op.

            Args:
                *inputs (Tensor): Multiple predicted feature maps.
                num_class (int): Number of classes.
                num_box (int): Number of box per grid.
                confidence_threshold (float): Threshold of object
                    score.
                nms_threshold (float): IoU threshold for NMS.
                biases (List[float]: Base sizes to compute anchors
                    for each FPN.
                mask (List[float]): Used to select base sizes in
                    biases.
                anchors_scale (List[float]): Down-sampling scales of
                    each FPN layer, e.g.: [32, 16].
            """

            @staticmethod
            def forward(ctx, *args):
                # create dummpy output of shape [num_boxes, 6],
                # each row is [label, score, x1, y1, x2, y2]
                output = torch.rand(100, 6)
                return output

            @staticmethod
            def symbolic(g, *args):
                anchors_scale = args[-1]
                inputs = args[:len(anchors_scale)]
                assert len(args) == (len(anchors_scale) + 7)
                return g.op(
                    'mmdeploy::Yolov3DetectionOutput',
                    *inputs,
                    num_class_i=args[-7],
                    num_box_i=args[-6],
                    confidence_threshold_f=args[-5],
                    nms_threshold_f=args[-4],
                    biases_f=args[-3],
                    mask_f=args[-2],
                    anchors_scale_f=anchors_scale,
                    outputs=1)

        return Yolov3DetectionOutputOp.apply(*pred_maps, self.num_classes,
                                             num_box, confidence_threshold,
                                             iou_threshold, anchor_biases,
                                             bias_masks, self.featmap_strides)

    output = _create_yolov3_detection_output()
    return output
