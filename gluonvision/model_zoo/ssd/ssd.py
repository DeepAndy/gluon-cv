"""Single-shot Multi-box Detector.
"""
from __future__ import absolute_import
import mxnet as mx
from mxnet import ndarray as nd
from mxnet import gluon
from mxnet import autograd
from mxnet.gluon import nn
from mxnet.gluon import Block, HybridBlock
from mxnet.gluon.model_zoo import vision
from ..features import FeatureExpander
from .anchor import SSDAnchorGenerator
from ..predictors import ConvPredictor
from ..coders import MultiClassDecoder, NormalizedBoxCenterDecoder
from .target import SSDTargetGenerator
from .vgg_atrous import vgg16_atrous_300, vgg16_atrous_512
from ...utils import set_lr_mult

__all__ = ['ssd_300_vgg16_atrous', 'ssd_512_vgg16_atrous',
           'ssd_512_resnet18_v1', 'ssd_512_resnet50_v1']


class SSD(HybridBlock):
    """Single-shot Object Detection Network.

    Parameters
    ----------
    network : type
        Description of parameter `network`.
    base_size : type
        Description of parameter `base_size`.
    features : type
        Description of parameter `features`.
    num_filters : type
        Description of parameter `num_filters`.
    scale : type
        Description of parameter `scale`.
    ratios : type
        Description of parameter `ratios`.
    steps : type
        Description of parameter `steps`.
    classes : type
        Description of parameter `classes`.
    use_1x1_transition : type
        Description of parameter `use_1x1_transition`.
    use_bn : type
        Description of parameter `use_bn`.
    reduce_ratio : type
        Description of parameter `reduce_ratio`.
    min_depth : type
        Description of parameter `min_depth`.
    global_pool : type
        Description of parameter `global_pool`.
    pretrained : type
        Description of parameter `pretrained`.
    iou_thresh : type
        Description of parameter `iou_thresh`.
    neg_thresh : type
        Description of parameter `neg_thresh`.
    negative_mining_ratio : type
        Description of parameter `negative_mining_ratio`.
    stds : type
        Description of parameter `stds`.
    nms_thresh : type
        Description of parameter `nms_thresh`.
    nms_topk : type
        Description of parameter `nms_topk`.
    force_nms : type
        Description of parameter `force_nms`.
    anchor_alloc_size : type
        Description of parameter `anchor_alloc_size`.

    """
    def __init__(self, network, base_size, features, num_filters, scale, ratios,
                 steps, classes, use_1x1_transition=True, use_bn=True,
                 reduce_ratio=1.0, min_depth=128, global_pool=False, pretrained=0,
                 iou_thresh=0.5, neg_thresh=0.5, negative_mining_ratio=3,
                 stds=(0.1, 0.1, 0.2, 0.2), nms_thresh=0, nms_topk=-1, force_nms=False,
                 anchor_alloc_size=128, **kwargs):

        super(SSD, self).__init__(**kwargs)
        if network is None:
            num_layers = len(ratios)
        else:
            num_layers = len(features) + len(num_filters) + int(global_pool)
        assert len(scale) == 2, "Must specify scale as (min_scale, max_scale)."
        min_scale, max_scale = scale
        sizes = [min_scale + (max_scale - min_scale) * i / (num_layers - 1)
                 for i in range(num_layers)] + [1.0]
        sizes = [x * base_size for x in sizes]
        sizes = list(zip(sizes[:-1], sizes[1:]))
        assert isinstance(ratios, list), "Must provide ratios as list or list of list"
        if not isinstance(ratios[0], (tuple, list)):
            ratios = ratios * num_layers  # propagate to all layers if use same ratio
        assert num_layers == len(sizes) == len(ratios), \
            "Mismatched (number of layers) vs (sizes) vs (ratios): {}, {}, {}".format(
                num_layers, len(sizes), len(ratios))
        assert num_layers > 0, "SSD require at least one layer, suggest multiple."
        self._num_layers = num_layers
        self.num_classes = classes + 1
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.force_nms = force_nms
        self.target = set([SSDTargetGenerator(
                iou_thresh=iou_thresh, neg_thresh=neg_thresh,
                negative_mining_ratio=negative_mining_ratio, stds=stds)])

        with self.name_scope():
            if network is None:
                # use fine-grained manually designed block as features
                self.features = features(pretrained=pretrained)
            else:
                self.features = FeatureExpander(
                    network=network, outputs=features, num_filters=num_filters,
                    use_1x1_transition=use_1x1_transition,
                    use_bn=use_bn, reduce_ratio=reduce_ratio, min_depth=min_depth,
                    global_pool=global_pool, pretrained=(pretrained > 0))
            self.class_predictors = nn.HybridSequential()
            self.box_predictors = nn.HybridSequential()
            self.anchor_generators = nn.HybridSequential()
            asz = anchor_alloc_size
            for i, s, r, st in zip(range(num_layers), sizes, ratios, steps):
                self.anchor_generators.add(SSDAnchorGenerator(i, s, r, st, (asz, asz)))
                asz = asz // 2
                num_anchors = self.anchor_generators[-1].num_depth
                self.class_predictors.add(ConvPredictor(num_anchors * self.num_classes))
                self.box_predictors.add(ConvPredictor(num_anchors * 4))
            self.bbox_decoder = NormalizedBoxCenterDecoder(stds)
            self.cls_decoder = MultiClassDecoder()

    def set_nms(self, nms_thresh=0, nms_topk=-1, force_nms=False):
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.force_nms = force_nms

    @property
    def target_generator(self):
        return list(self.target)[0]

    def hybrid_forward(self, F, x):
        features = self.features(x)
        cls_preds = [F.flatten(F.transpose(cp(feat), (0, 2, 3, 1)))
            for feat, cp in zip(features, self.class_predictors)]
        box_preds = [F.flatten(F.transpose(bp(feat), (0, 2, 3, 1)))
            for feat, bp in zip(features, self.box_predictors)]
        anchors = [F.reshape(ag(feat), shape=(1, -1))
            for feat, ag in zip(features, self.anchor_generators)]
        cls_preds = F.concat(*cls_preds, dim=1).reshape((0, -1, self.num_classes))
        box_preds = F.concat(*box_preds, dim=1).reshape((0, -1, 4))
        anchors = F.concat(*anchors, dim=1).reshape((1, -1, 4))
        if autograd.is_recording():
            return [cls_preds, box_preds, anchors]
        bboxes = self.bbox_decoder(box_preds, anchors)
        cls_ids, scores = self.cls_decoder(F.softmax(cls_preds))
        result = F.concat(
            cls_ids.expand_dims(axis=-1), scores.expand_dims(axis=-1), bboxes, dim=-1)
        if self.nms_thresh > 0 and self.nms_thresh < 1:
            result = F.contrib.box_nms(
                result, overlap_thresh=self.nms_thresh, topk=self.nms_topk,
                id_index=0, score_index=1, coord_start=2, force_suppress=self.force_nms)
        ids = F.slice_axis(result, axis=2, begin=0, end=1)
        scores = F.slice_axis(result, axis=2, begin=1, end=2)
        bboxes = F.slice_axis(result, axis=2, begin=2, end=6)
        return ids, scores, bboxes

def get_ssd(name, base_size, features, filters, scale, ratios, steps,
            classes=20, pretrained=0, **kwargs):
    """Get SSD models.

    Parameters
    ----------
    name : str
        Model name
    base_size : int

    """
    net = SSD(name, base_size, features, filters, scale, ratios, steps,
              pretrained=pretrained, classes=classes, **kwargs)
    if pretrained > 1:
        # load trained ssd model
        raise NotImplementedError("Loading pretrained model for detection is not finished.")
    set_lr_mult(net, ".*_bias", 2.0)  #TODO(zhreshold): fix pattern
    return net

def ssd_300_vgg16_atrous(pretrained=0, classes=20, **kwargs):
    net = get_ssd(None, 300, features=vgg16_atrous_300,
                  filters=None, scale=[0.1, 0.95],
                  ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 3 + [[1, 2, 0.5]] * 2,
                  steps=[8, 16, 32, 64, 100, 300],
                  classes=classes, pretrained=pretrained, **kwargs)
    return net

def ssd_512_vgg16_atrous(pretrained=0, classes=20, **kwargs):
    net = get_ssd(None, 512, features=vgg16_atrous_512,
                  filters=None, scale=[0.1, 0.95],
                  ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 4 + [[1, 2, 0.5]] * 2,
                  steps=[8, 16, 32, 64, 128, 256, 512],
                  classes=classes, pretrained=pretrained, **kwargs)
    return net

def ssd_512_resnet18_v1(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v1 18 layers.

    """
    return get_ssd('resnet18_v1', 512,
                   features=['stage3_activation1', 'stage4_activation1'],
                   filters=[512, 512, 256, 256], scale=[0.1, 0.95],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 4 + [[1, 2, 0.5]] * 2,
                   steps=[8, 16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)

def ssd_512_resnet50_v1(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v1 50 layers.

    """
    return get_ssd('resnet50_v1', 512,
                   features=['stage3_activation5', 'stage4_activation2'],
                   filters=[512, 512, 256, 256], scale=[0.1, 0.95],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 5,
                   steps=[16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)