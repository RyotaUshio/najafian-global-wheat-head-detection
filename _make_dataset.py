import torch
import torchvision
import cv2
import albumentations as A
import numpy as np
import skimage.measure
import pathlib
import re
import argparse
import dataclasses
from typing import ClassVar, Callable, Iterable, Union, Dict, List
import time
from collections import OrderedDict, defaultdict, namedtuple
import contextlib

import utils

_patterns = [
  re.compile(r'(.+)_(\d{4})(\..+)'),
  re.compile(r'(.+)(\..+)')
]

def parse_fname(fname):
    fname = pathlib.Path(fname).name
    for i, pattern in enumerate(_patterns):
        m = pattern.match(fname)
        if m:
            break
        if i == len(_patterns) - 1:
            raise Exception(f'An invalid file name "{fname}" was given')

    ret = {k: None for k in ['stem', 'time', 'extension']}
    
    if i == 0:
      keys = ['stem', 'time', 'extension']
    elif i == 1:
      keys = ['stem', 'extension']
    else:
        assert False

    ret.update({k: v for k, v in zip(keys, m.groups())})
    return ret    
  
def get_bbox(masks):
    return_list = True
    if masks.ndim <= 2:
        return_list = False
        masks = torch.unsqueeze(masks, 0)

    boxes = torchvision.ops.masks_to_boxes(masks).to(int)
    ret = []
    for box in boxes:
        left, top, right, bottom = box
        ret.append(dict(left=left.item(), right=right.item(), top=top.item(), bottom=bottom.item()))
    return ret if return_list else ret[0]

def bbox_to_pascal_voc(bbox: dict):
    """(x_min, y_min, x_max, y_max)
    """
    return bbox['left'], bbox['top'], bbox['right'], bbox['bottom']

def bbox_to_albumentations(bbox: dict, *, image_width: int, image_height: int):
    """normalized (x_min, y_min, x_max, y_max)
    """
    x_min, y_min, x_max, y_max = bbox_to_pascal_voc(bbox)
    x_min /= image_width
    y_min /= image_height
    x_max /= image_width
    y_max /= image_height
    return x_min, y_min, x_max, y_max

def bbox_to_coco(bbox: dict):
    """(x_min, y_min, width, height)
    """
    x_min, y_min, x_max, y_max = bbox_to_pascal_voc(bbox)
    width = x_max - x_min
    height = y_max - y_min
    return x_min, y_min, width, height
        
def bbox_to_yolo(bbox: dict, *, image_width: int, image_height: int):
    """normalized (x_center, y_center, width, height)
    """
    x_min, y_min, x_max, y_max = bbox_to_pascal_voc(bbox)
    x_center = 0.5 * (x_min + x_max)
    y_center = 0.5 * (y_min+ y_max)
    width = x_max - x_min
    height = y_max - y_min

    # normalize so that everything will be in [0, 1]
    x_center /= image_width
    y_center /= image_height
    width /= image_width
    height /= image_height

    return x_center, y_center, width, height

def make_classwise_mask(p_mask, n_class):
    """p_mask: path to .npy file which contains class-wise masks in the VOC format
    """
    labels = torch.as_tensor(np.load(p_mask))
    masks = []
    for i_class in range(n_class):
        classwise_mask = (labels == i_class + 1)
        masks.append(classwise_mask)
    return torch.stack(masks)

def make_objectwise_mask(classwise_masks, n_class):
    """classwise_masks: tensor of shape n_class x H x W (stack of masks)
    """
    ret = dict()
    for i_class in range(n_class):
        objectwise_masks = skimage.measure.label(classwise_masks[i_class].cpu()) # labels connected components
        object_id = np.unique(objectwise_masks)[1:] # ignore background = 0
        objectwise_masks = (objectwise_masks == object_id.reshape(-1, 1, 1))
        ret.update({i_class: torch.as_tensor(objectwise_masks)})
    return ret
    

@dataclasses.dataclass
class Video:
    p_video: pathlib.Path

    stem: str = dataclasses.field(init=False)
    capture: cv2.VideoCapture = dataclasses.field(init=False, default=None)
    is_open: bool = dataclasses.field(init=False, default=False)

    def __post_init__(self):
        parsed = parse_fname(self.p_video)
        self.stem = parsed['stem']

    def make_capture(self) -> None:
        if self.is_open:
            return 
        self.capture = cv2.VideoCapture(str(self.p_video))
        if not self.capture.isOpened():
            raise Exception(f'Cannot open file {p_video}')
        self.is_open = True

    def close(self) -> None:
        self.capture.release()
        self.is_open = False
        assert not self.capture.isOpened()

    @contextlib.contextmanager
    def open(self):
        try:
            self.make_capture()
            yield
        finally:
            self.close()

    def read_frame(self, i_frame, device=None):
        frame = utils.read_frame(self.capture, i_frame, device=device)
        return frame

    def __len__(self):
        if not self.is_open:
            raise ValueError('cannot get the number of frames from closed video')
        n_frame = self.capture.get(cv2.CAP_PROP_FRAME_COUNT)
        return int(n_frame)

    def random_read(self, device=None, noexcept=True):
        n_frame = len(self)
        while True:
            try:
                i_frame = np.random.choice(n_frame)
                frame = self.read_frame(i_frame, device)
            except RuntimeError as e:
                print('Something went wrong while reading frames from a video:')
                if noexcept:
                    print(f'  {e}')
                else:
                    raise e
            else:
                break
        return frame


@dataclasses.dataclass(repr=False)
class BackgroundVideo(Video):
    pass


@dataclasses.dataclass(repr=False)
class FieldVideo(Video):
    p_rep_images: dataclasses.InitVar[pathlib.Path]
    p_masks: dataclasses.InitVar[pathlib.Path]
    rep_image_extension: dataclasses.InitVar[str]
    classes: List[str]
    device: dataclasses.InitVar[Union[str, torch.device]] = 'cpu'

    rep_images: List['RepImage'] = dataclasses.field(init=False, default_factory=list)
    orig_images: List[torch.Tensor] = dataclasses.field(init=False)

    def __post_init__(
        self, 
        p_rep_images: pathlib.Path, 
        p_masks: pathlib.Path, 
        rep_image_extension: str, 
        device: Union[str, torch.device]
    ):
        super().__post_init__()
        # make sure pathes are pathlib.Path objects
        self.p_video = pathlib.Path(self.p_video)
        p_rep_images = pathlib.Path(p_rep_images)
        p_masks = pathlib.Path(p_masks)
        assert isinstance(rep_image_extension, str)

        if not rep_image_extension.startswith('.'):
            rep_image_extension += '.' + rep_image_extension
        for p_rep_image in p_rep_images.glob(f'{self.stem}_*{rep_image_extension}'):
            p_mask = p_masks / (p_rep_image.stem + '.npy')
            image = RepImage(p_image=p_rep_image, p_mask=p_mask, video=self, classes=self.classes, device=device, stem=self.stem)
            self.rep_images.append(image)
        self.orig_images = [rep_image.orig_image for rep_image in self.rep_images] # make copy for rep_image_as()

    @contextlib.contextmanager
    def rep_images_as(self, tmp_images):
        """context manager to set temporary rep-images

        The passed images must be \"spatially compatible\" with the original images. 
        You will get a broken result if **tmp_images** are output of data augmentation
        where coordinates are not preserved.
        """
        assert len(tmp_images) == len(self.orig_images)
        try:
            for rep_image, tmp_image in zip(self.rep_images, tmp_images):
                rep_image.set_image(tmp_image)
            yield
        finally:
            for rep_image, orig_image in zip(self.rep_images, self.orig_images):
                rep_image.set_image(orig_image)

    def get_objects(self, **kwargs):
        objects = []
        for rep_image in self.rep_images:
            objects += rep_image.get_objects(**kwargs)
        return objects


@dataclasses.dataclass(repr=False)
class RepImage:
    p_image: pathlib.Path
    p_mask: pathlib.Path
    video: FieldVideo
    classes: List[str]
    device: torch.device = 'cpu'
    stem: str = None

    timestamp: time.struct_time = dataclasses.field(init=False)
    image: torch.Tensor = dataclasses.field(init=False)
    orig_image: torch.Tensor = dataclasses.field(init=False)
    height: int = dataclasses.field(init=False)
    width: int = dataclasses.field(init=False)
    classwise_mask: torch.Tensor = dataclasses.field(init=False)
    objectwise_mask: dict = dataclasses.field(init=False)
    objects: dict = dataclasses.field(init=False)

    def __post_init__(self):
        self.device = torch.device(self.device)
        parsed = parse_fname(self.p_image)
        self.timestamp = time.strptime(parsed['time'], '%M%S')
        self.image = utils.read_image(self.p_image, self.device)
        self.height = self.image.size(-2)
        self.width = self.image.size(-1)
        # read mask
        n_class = len(self.classes)
        classwise_mask = make_classwise_mask(self.p_mask, n_class)
        objectwise_mask = make_objectwise_mask(classwise_mask, n_class)
        # send masks to device
        classwise_mask = classwise_mask.to(self.device)
        for i_class, masks in objectwise_mask.items():
            objectwise_mask.update({i_class: masks.to(self.device)})

        # construct foreground objects
        self.objects = {}
        for i_class, class_name in enumerate(self.classes):
            obj_list = []
            masks = objectwise_mask[i_class] # obj-wise masks of objects of i-th class
            for mask in masks:
                obj = ForegroundObject(
                    rep_image=self,
                    mask=mask,
                    i_class=i_class,
                    class_name=class_name
                )        
                obj_list.append(obj)
            self.objects.update({i_class: obj_list})

        self.classwise_mask = classwise_mask
        self.objectwise_mask = objectwise_mask
        self.orig_image = self.image.detach().clone() # make copy

    def to(self, device, *, mask=False) -> None:
        self.image = self.image.to(device)
        self.device = self.image.device
        if mask:
            self.classwise_mask = self.classwise_mask.to(device)
            for i_class, masks in self.objectwise_mask.items():
                self.objectwise_mask.update({i_class: masks.to(device)})

    def set_image(self, new_image: torch.Tensor) -> None:
        """set a new image as the object's image attribute and make a new cropped image.
        new_image must be consistent with the old one in spatial information, i.e. you will
        get a nonsense result if you pass an image which went through data augmentation 
        where spatial information is not preserved.
        """
        assert new_image.size() == self.image.size(), 'cannot set a tensor of incompatible size'
        self.image[:] = new_image # substitute in-place

    @contextlib.contextmanager
    def image_as(self, tmp_image):
        """context manager to set a temporary image

        The passed image must be \"spatially compatible\" with the original image. 
        You will get a broken result if **tmp_image** is output of data augmentation
        where coordinates are not preserved.
        """
        try:
            self.set_image(tmp_image)
            yield
        finally:
            self.set_image(self.orig_image)

    def get_objects(self, **kwargs):
        keys = ['i_class', 'class_name']
        if not kwargs:
            objects = []
            for i_class in range(len(self.classes)):
                objects += self.get_objects(i_class=i_class)
            return objects

        if len(kwargs) != 1 or all([not key in kwargs for key in keys]):
            raise ValueError(f'{self.__class__.__name__}.get_objects requires exactly 1 keyword argument: i_class or class_name')
        i_class = kwargs.get('i_class')
        if i_class is None:
            i_class = self.classes.index(kwargs['class_name'])
        return self.objects[i_class]


@dataclasses.dataclass
class ForegroundObject:
    __random_rotate: ClassVar[Callable] = torchvision.transforms.RandomRotation(degrees=180, expand=True, fill=0)
    __bbox_formats: ClassVar[List[str]] = ['pascal_voc', 'albumentations', 'coco', 'yolo']

    rep_image: RepImage
    mask: torch.Tensor
    i_class: int
    class_name: str

    bbox: dict = dataclasses.field(init=False)
    image_cropped: torch.Tensor = dataclasses.field(init=False)
    mask_cropped: torch.Tensor = dataclasses.field(init=False)

    def __post_init__(self):
        self.bbox = get_bbox(self.mask)
        top, bottom, left, right = self.bbox['top'], self.bbox['bottom'], self.bbox['left'], self.bbox['right']
        self.image_cropped = self.crop(self.rep_image.image) # self.image[:, top:bottom, left:right]
        self.mask_cropped = self.crop(self.mask) # self.mask[top:bottom, left:right]

    def crop(self, tensor):
        top, bottom, left, right = self.bbox['top'], self.bbox['bottom'], self.bbox['left'], self.bbox['right']
        tensor = tensor.transpose(0, -2).transpose(1, -1)
        tensor = tensor[top:bottom, left:right]
        tensor = tensor.transpose(1, -1).transpose(0, -2)
        return tensor

    def bbox_to(self, format):
        if format not in self.__bbox_formats:
            raise ValueError(f'invalid format "{format}" was given')
        func = getattr(self, 'bbox_to_' + format)
        return func()

    def bbox_to_pascal_voc(self):
        return bbox_to_pascal_voc(self.bbox)

    def bbox_to_albumentations(self):
        return bbox_to_albumentations(self.bbox, image_width=self.rep_image.width, image_height=self.rep_image.height)

    def bbox_to_coco(self):
        return bbox_to_coco(self.bbox)

    def bbox_to_yolo(self):
        return bbox_to_yolo(self.bbox, image_width=self.rep_image.width, image_height=self.rep_image.height)

    def random_place(self, background):
        """Randomly place the object on the given background image. This is an in-place operation.
        
        Parameters
        ----------
        background: array or tensor
          An image on which the object is placed
        """
        rotated = self.__random_rotate(
            torch.cat(
                [self.image_cropped, torch.unsqueeze(self.mask_cropped, 0)]
            )
        )
        image_cropped, mask_cropped = torch.split(rotated, [3, 1], dim=0)
        mask_cropped = torch.squeeze(mask_cropped, dim=0).to(torch.bool)
        _, h, w = image_cropped.size() # size of cropped region after rotation
        
        top = np.random.randint(0, self.rep_image.height - h)
        left = np.random.randint(0, self.rep_image.width - w)
        background[:, top:top+h, left:left+w][:, mask_cropped] = image_cropped[:, mask_cropped]
        
        # bounding box info
        bbox = get_bbox(mask_cropped)
        bbox['top'] += top
        bbox['bottom'] += top
        bbox['left'] += left
        bbox['right'] += left
        return bbox
    

class YoloLabel:
    def __init__(self, image):
        self.bboxes = []
        self.image_width = float(image.size(-1))
        self.image_height = float(image.size(-2))
        self.dicts = []

    def add(self, *, obj: ForegroundObject=None, i_class: int=None, bbox: dict=None):
        """call with signature of add(obj=obj) or add(i_class=i_class, bbox=bbox)
        """
        if obj is not None:
            assert i_class is None and bbox is None
            x_center, y_center, width, height = obj.bbox_to_yolo()    
            i_class = obj.i_class
        else:
            assert i_class is not None and bbox is not None
            x_center, y_center, width, height = bbox_to_yolo(bbox, image_width=self.image_width, image_height=self.image_height)
        self.bboxes.append(
            OrderedDict(
                i_class=i_class, 
                x_center=x_center,
                y_center=y_center,
                width=width,
                height=height
            )
        )
        self.dicts.append(bbox)

    def save(self, fname):
        with open(fname, 'w') as f:
            for label in self.bboxes:
                row = ' '.join([str(value) for value in label.values()])
                row += '\n'
                f.write(row)

    def to_tensor(self):
        return torch.tensor([[bbox['left'], bbox['top'], bbox['right'], bbox['bottom']] for bbox in self.dicts])

    def class_name_list(self, classes):
        return [classes[bbox['i_class']] for bbox in self.bboxes]


def foreground_augmentation(field_video, format='yolo'):
    images = []
    transform = utils.make_foreground_augmentation()
    for rep_image in field_video.rep_images:
        transformed = transform(image=utils.tensorimage_to_numpy(rep_image.image))
        image = transformed['image']
        image = utils.numpyimage_to_tensor(image, device=rep_image.image.device)
        images.append(image)
    return images

def background_augmentation(frame):
    transform = utils.make_background_augmentation()
    transformed = transform(image=utils.tensorimage_to_numpy(frame))
    image = transformed['image']
    image = utils.numpyimage_to_tensor(image, device=frame.device)
    return image

def synthesize(*, frame, field_video, prob=None):
    """randomly place foreground objects onto the given frame in-place

    frame: a frame taken from a background video
    field_video: a field video
    prob: the probablity that a foreground object will be picked from each class
    """
    if not field_video.rep_images:
        raise ValueError(f'field_video {field_video.p_video} has no rep_image') 
    rng = np.random.default_rng()
    n_obj = int(rng.normal(loc=6, scale=1.5)) # 謎のヒューリティクス
    n_class = len(field_video.classes)
    label = YoloLabel(frame)
    rep_images_aug = foreground_augmentation(field_video)
    frame_aug = background_augmentation(frame)

    with field_video.rep_images_as(rep_images_aug):
        for _ in range(n_obj):
            if prob is None:
                objects = field_video.get_objects()
            else:
                while True:
                    i_class = rng.choice(range(n_class), p=prob)
                    objects = field_video.get_objects(i_class=i_class)
                    if objects: # retry if empty
                        break
            
            obj = rng.choice(objects)
            bbox = obj.random_place(frame_aug)
            label.add(i_class=obj.i_class, bbox=bbox)

    return frame_aug, label

def generate_rotated_rep_image(rep_image: RepImage, *, pathes: namedtuple, bbox: bool, counter: Callable):
    n_class = len(rep_image.classes)
    concat = torch.cat([rep_image.image, rep_image.classwise_mask])
    
    for degree in range(360):
        image, label = _generate_rotated_rep_image_impl(concat, degree=degree, n_class=n_class)
        output_stem = f'{rep_image.p_image.stem}_{degree:03}degrees'
        output_image_name = pathes.output_domain_adaptation_images_dir / (output_stem + rep_image.p_image.suffix)
        output_label_name = pathes.output_domain_adaptation_labels_dir / (output_stem + '.txt')
        utils.save_image(image, output_image_name)
        label.save(output_label_name)
        if bbox:
            output_labeled_image_name = pathes.output_domain_adaptation_labeled_images_dir / output_image_name.name
            utils.save_labeled_image(image, label, output_labeled_image_name, rep_image.classes)
        counter()
            
def _generate_rotated_rep_image_impl(concat, *, degree, n_class):
    """image: Tensor (C, H, W)
    classwise_mask: Tensor (n_class, H, W)
    """
    rotated = torchvision.transforms.functional.rotate(
        concat, degree
    )
    image, classwise_mask = torch.split(rotated, [3, n_class], dim=0)
    objectwise_mask = make_objectwise_mask(classwise_mask, n_class)
    label = YoloLabel(image)
        
    # bounding box info
    for i_class in range(n_class):
        bboxes = get_bbox(objectwise_mask[i_class])
        for bbox in bboxes:
            label.add(i_class=i_class, bbox=bbox)

    return image, label