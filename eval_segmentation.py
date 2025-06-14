import time
import os
import re
import psutil
import click
import pickle
from pathlib import Path
from typing import Union
import torch
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
import json
import numpy as np
import pandas as pd
import torchvision
import torchvision.transforms as torch_tf
import torchvision.transforms as transforms
import torch.nn.functional as F
from PIL import Image
import cv2
import dnnlib
from torch_utils import misc
from torch_utils import training_stats
# from generate import generate_fn
from scipy import io
from sklearn.metrics import adjusted_rand_score
from metrics.segmentation import np_average_segcover

#----------------------------------------------------------------------------

class SynSegDataset(torch.utils.data.Dataset):
    def __init__(self,
        path,
        indices     = None,
        transforms  = None,
        size        = None,
        crop_ratio  = None,
    ):
        from glob import glob
        filenames = sorted(glob(os.path.join(path, "img", "*.png")))
        if indices is None:
            indices = range(len(filenames))
        self.filenames = [filenames[i] for i in indices]
        self.maskfiles = [os.path.join(path, 'mask', os.path.basename(filename)) for filename in self.filenames]
        # self.maskfiles = [os.path.join(path, f'mask/{i:06d}.png') for i in indices]

        if transforms is None:
            if size is None:
                transforms = torch_tf.Compose([torch_tf.ToTensor(), torch_tf.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])
            else:
                transforms = torch_tf.Compose([
                    torch_tf.Resize((int(size), int(size))),
                    torch_tf.ToTensor(),
                    torch_tf.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])
        self.size = size
        if crop_ratio is not None:
            assert 0 < crop_ratio <= 1.0
        self.crop_ratio = crop_ratio
        self.transforms = transforms
        self.resolution = self[0][0].size(1)

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, idx):
        img = Image.open(self.filenames[idx]).convert("RGB")
        if self.crop_ratio is None or self.crop_ratio == 1.0:
            img = img
        else:
            img = torchvision.transforms.CenterCrop(int(self.size*self.crop_ratio))(img)
        img_tensor = self.transforms(img)

        # standard.
        mask = Image.open(self.maskfiles[idx]).convert("RGB")
        if self.crop_ratio is None or self.crop_ratio == 1.0:
            mask = mask
        else:
            mask = torchvision.transforms.CenterCrop(int(self.size*self.crop_ratio))(mask)
        mask_tensor = (self.transforms(mask)[0] > 0.).long()
        return img_tensor, mask_tensor

#----------------------------------------------------------------------------

class RealSegTestSet(torch.utils.data.Dataset):
    def __init__(self,
                 rootdir,
                 scale       = 128,
                 transforms  = None
                 ):
        self.rootdir = rootdir
        self.image_dir = os.path.join(rootdir, 'images')
        self.mask_dir = os.path.join(rootdir, 'masks')
        filenames = [str(f) for f in sorted(Path(self.image_dir).rglob('*'))
                     if str(f).split('.')[-1].lower() in ['jpg', 'png', 'jpeg']
                     and os.path.isfile(f)]
        self.indices = [os.path.relpath(x, self.image_dir) for x in filenames]

        if transforms is None:
            transforms = torch_tf.Compose([torch_tf.Resize(scale), torch_tf.CenterCrop(scale), torch_tf.ToTensor()])
        self.transforms = transforms

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, idx):
        filename = self.indices[idx]

        # Load images
        img = Image.open(os.path.join(self.image_dir, filename)).convert("RGB")
        img_tensor = self.transforms(img)
        img_tensor = 2 * img_tensor - 1

        # Load masks
        mask = Image.open(os.path.join(self.mask_dir, filename[:-3] + 'png'))
        mask_tensor = self.transforms(mask)
        if mask_tensor.ndim > 2:
            mask_tensor = mask_tensor[0]
        mask_tensor = (mask_tensor > 0.5).long()

        return img_tensor, mask_tensor

#----------------------------------------------------------------------------
# Real Dataset
#----------------------------------------------------------------------------
class CubDataset(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.bbox_meta, self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        """ Returns a dictionary with image filename as
        'key' and its bounding box coordinates as 'value' """

        data_dir = self.ROOT_DIR

        bbox_path = os.path.join(data_dir, 'bounding_boxes.txt')
        df_bounding_boxes = pd.read_csv(bbox_path,
                                        delim_whitespace=True,
                                        header=None).astype(int)

        filepath = os.path.join(data_dir, 'images.txt')
        df_filenames = \
            pd.read_csv(filepath, delim_whitespace=True, header=None)

        filenames = df_filenames[1].tolist()
        print('Total filenames: ', len(filenames), filenames[0])
        filename_bbox = {img_file[:-4]: [] for img_file in filenames}
        numImgs = len(filenames)

        splits = np.loadtxt(os.path.join(data_dir, 'train_val_test_split.txt'), int)

        for i in range(0, numImgs):
            bbox = df_bounding_boxes.iloc[i][1:].tolist()
            key = filenames[i][:-4]
            filename_bbox[key] = bbox

        filenames = [fname[:-4] for fname in filenames]

        if self.data_split == 0: # training split
            filenames = np.array(filenames)
            filenames = filenames[splits[:, 1] == 0]
            filename_bbox_ = {fname: filename_bbox[fname] for fname in filenames}
        elif self.data_split == 2: # testing split
            filenames = np.array(filenames)
            filenames = filenames[splits[:, 1] == 2]
            filename_bbox_ = {fname: filename_bbox[fname] for fname in filenames}
        elif self.data_split == -1: # all dataset
            filenames = filenames.copy()
            filename_bbox_ = filename_bbox

        print('Filtered filenames: ', len(filenames))
        return filename_bbox_, filenames

    def load_item(self, index):
        key = self.file_meta[index]
        bbox = self.bbox_meta[key]

        data_dir = self.ROOT_DIR

        img_path = '%s/images/%s.jpg' % (data_dir, key)
        img = self.load_imgs(img_path, bbox)

        seg_path = '%s/segmentations/%s.png' % (data_dir, key)
        seg = self.load_segs(seg_path, bbox)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path, bbox):
        img = Image.open(img_path).convert('RGB')
        width, height = img.size

        if bbox is not None:
            r = int(np.maximum(bbox[2], bbox[3]) * 0.75)
            center_x = int((2 * bbox[0] + bbox[2]) / 2)
            center_y = int((2 * bbox[1] + bbox[3]) / 2)
            y1 = np.maximum(0, center_y - r)
            y2 = np.minimum(height, center_y + r)
            x1 = np.maximum(0, center_x - r)
            x2 = np.minimum(width, center_x + r)

        cimg = img.crop([x1, y1, x2, y2])
        return self.transform(cimg)

    def load_segs(self, seg_path, bbox):
        # img = Image.open(seg_path).convert('1')

        img = Image.open(seg_path)

        width, height = img.size

        if bbox is not None:
            r = int(np.maximum(bbox[2], bbox[3]) * 0.75)
            center_x = int((2 * bbox[0] + bbox[2]) / 2)
            center_y = int((2 * bbox[1] + bbox[3]) / 2)
            y1 = np.maximum(0, center_y - r)
            y2 = np.minimum(height, center_y + r)
            x1 = np.maximum(0, center_x - r)
            x2 = np.minimum(width, center_x + r)

        cimg = img.crop([x1, y1, x2, y2])

        img_tensor = (self.transform_seg(cimg) > 0.5).long()

        if img_tensor.ndim > 2:
            img_tensor = img_tensor[0]

        # return self.transform_seg(cimg)
        return img_tensor

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item

class CubDatasetCenterCrop(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self._aspect_ratio = 1.0
        self._crop = 'center'

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.bbox_meta, self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        """ Returns a dictionary with image filename as
        'key' and its bounding box coordinates as 'value' """

        data_dir = self.ROOT_DIR

        bbox_path = os.path.join(data_dir, 'bounding_boxes.txt')
        df_bounding_boxes = pd.read_csv(bbox_path,
                                        delim_whitespace=True,
                                        header=None).astype(int)

        filepath = os.path.join(data_dir, 'images.txt')
        df_filenames = \
            pd.read_csv(filepath, delim_whitespace=True, header=None)

        filenames = df_filenames[1].tolist()
        print('Total filenames: ', len(filenames), filenames[0])
        filename_bbox = {img_file[:-4]: [] for img_file in filenames}
        numImgs = len(filenames)

        splits = np.loadtxt(os.path.join(data_dir, 'train_val_test_split.txt'), int)

        for i in range(0, numImgs):
            bbox = df_bounding_boxes.iloc[i][1:].tolist()
            key = filenames[i][:-4]
            filename_bbox[key] = bbox

        filenames = [fname[:-4] for fname in filenames]

        if self.data_split == 0: # training split
            filenames = np.array(filenames)
            filenames = filenames[splits[:, 1] == 0]
            filename_bbox_ = {fname: filename_bbox[fname] for fname in filenames}
        elif self.data_split == 2: # testing split
            filenames = np.array(filenames)
            filenames = filenames[splits[:, 1] == 2]
            filename_bbox_ = {fname: filename_bbox[fname] for fname in filenames}
        elif self.data_split == -1: # all dataset
            filenames = filenames.copy()
            filename_bbox_ = filename_bbox

        print('Filtered filenames: ', len(filenames))
        return filename_bbox_, filenames

    def load_item(self, index):
        key = self.file_meta[index]
        bbox = self.bbox_meta[key]

        data_dir = self.ROOT_DIR

        img_path = '%s/images/%s.jpg' % (data_dir, key)
        img = self.load_imgs(img_path, bbox)

        seg_path = '%s/segmentations/%s.png' % (data_dir, key)
        seg = self.load_segs(seg_path, bbox)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path, bbox):
        img = Image.open(img_path).convert('RGB')
        # width, height = img.size
        #
        # if bbox is not None:
        #     r = int(np.maximum(bbox[2], bbox[3]) * 0.75)
        #     center_x = int((2 * bbox[0] + bbox[2]) / 2)
        #     center_y = int((2 * bbox[1] + bbox[3]) / 2)
        #     y1 = np.maximum(0, center_y - r)
        #     y2 = np.minimum(height, center_y + r)
        #     x1 = np.maximum(0, center_x - r)
        #     x2 = np.minimum(width, center_x + r)
        #
        # cimg = img.crop([x1, y1, x2, y2])

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        return self.transform(image)

    def load_segs(self, seg_path, bbox):
        img = Image.open(seg_path).convert('1')
        # width, height = img.size
        #
        # if bbox is not None:
        #     r = int(np.maximum(bbox[2], bbox[3]) * 0.75)
        #     center_x = int((2 * bbox[0] + bbox[2]) / 2)
        #     center_y = int((2 * bbox[1] + bbox[3]) / 2)
        #     y1 = np.maximum(0, center_y - r)
        #     y2 = np.minimum(height, center_y + r)
        #     x1 = np.maximum(0, center_x - r)
        #     x2 = np.minimum(width, center_x + r)
        #
        # cimg = img.crop([x1, y1, x2, y2])
        # return self.transform_seg(cimg)

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        return (self.transform_seg(image) > 0.5).long()

    def _get_crop_params(self, x):
        width, height = x.size

        if self._crop == 'center':
            #
            i, j = 0, 0
            if height > width * self._aspect_ratio: # height is longer than expected
                i = int((height - width * self._aspect_ratio) // 2)
                height = int(width * self._aspect_ratio)

            elif height < width * self._aspect_ratio:
                j = int((width - height / self._aspect_ratio) // 2)
                width = int(height / self._aspect_ratio)

        elif self._crop == 'random':
            scale = np.random.uniform(*self._resize_scale)
            ww, hh = int(scale * width), int(scale * height) # Scale
            ww = min(ww, hh / self._aspect_ratio) # Crop to aspect_ratio
            hh = min(hh, ww * self._aspect_ratio)
            i = int(np.random.uniform(0, height - hh))
            j = int(np.random.uniform(0, width -ww))
            height, width = hh, ww

        else:
            raise NotImplementedError

        return i, j, height, width

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item

class DogDataset(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        sel_indices_tr = np.load('{}/data_tr_sel.npy'.format(self.ROOT_DIR))
        sel_indices_te = np.load('{}/data_te_sel.npy'.format(self.ROOT_DIR))

        if self.data_split == 0: # training split
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr]
        elif self.data_split == 2: # testing split
            filenames = ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        elif self.data_split == -1: # all dataset
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr] \
                        + ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        return filenames

    def load_item(self, index):
        key = self.file_meta[index]

        data_dir = self.ROOT_DIR

        img_path = '%s/%s_resized.png' % (data_dir, key)
        img = self.load_imgs(img_path)

        seg_path = '%s/%s_maskresized.png' % (data_dir, key)
        seg = self.load_segs(seg_path)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path):
        img = cv2.imread(img_path)
        img = Image.fromarray(img)

        return self.transform(img)

    def load_segs(self, seg_path):
        img = Image.open(seg_path).convert('1')

        # return self.transform_seg(img)
        return (self.transform_seg(img) > 0.5).long()

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item

class DogDatasetCenterCrop(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self._aspect_ratio = 1.0
        self._crop = 'center'

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        sel_indices_tr = np.load('{}/data_tr_sel.npy'.format(self.ROOT_DIR))
        sel_indices_te = np.load('{}/data_te_sel.npy'.format(self.ROOT_DIR))

        if self.data_split == 0: # training split
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr]
        elif self.data_split == 2: # testing split
            filenames = ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        elif self.data_split == -1: # all dataset
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr] \
                        + ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        return filenames

    def load_item(self, index):
        key = self.file_meta[index]

        data_dir = self.ROOT_DIR

        img_path = '%s/%s_resized.png' % (data_dir, key)
        img = self.load_imgs(img_path)

        seg_path = '%s/%s_maskresized.png' % (data_dir, key)
        seg = self.load_segs(seg_path)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path):
        img = cv2.imread(img_path)
        img = Image.fromarray(img)

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        return self.transform(image)

    def load_segs(self, seg_path):
        img = Image.open(seg_path).convert('1')

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        # return self.transform_seg(img)
        return (self.transform_seg(image) > 0.5).long()

    def _get_crop_params(self, x):
        width, height = x.size

        if self._crop == 'center':
            #
            i, j = 0, 0
            if height > width * self._aspect_ratio: # height is longer than expected
                i = int((height - width * self._aspect_ratio) // 2)
                height = int(width * self._aspect_ratio)

            elif height < width * self._aspect_ratio:
                j = int((width - height / self._aspect_ratio) // 2)
                width = int(height / self._aspect_ratio)

        elif self._crop == 'random':
            scale = np.random.uniform(*self._resize_scale)
            ww, hh = int(scale * width), int(scale * height) # Scale
            ww = min(ww, hh / self._aspect_ratio) # Crop to aspect_ratio
            hh = min(hh, ww * self._aspect_ratio)
            i = int(np.random.uniform(0, height - hh))
            j = int(np.random.uniform(0, width -ww))
            height, width = hh, ww

        else:
            raise NotImplementedError

        return i, j, height, width

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item

class CarDataset(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        sel_indices_tr = np.load('{}/data_mrcnn_train_select.npy'.format(self.ROOT_DIR))
        sel_indices_te = np.load('{}/data_mrcnn_test_select.npy'.format(self.ROOT_DIR))

        if self.data_split == 0: # training split
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr]
        elif self.data_split == 2: # testing split
            filenames = ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        elif self.data_split == -1: # all dataset
            filenames = ['data_mrcnn/train/resized/{}'.format(token) for token in sel_indices_tr] \
                        + ['data_mrcnn/test/resized/{}'.format(token) for token in sel_indices_te]
        return filenames

    def load_item(self, index):
        key = self.file_meta[index]

        data_dir = self.ROOT_DIR

        img_path = '%s/%s_resized.png' % (data_dir, key)
        img = self.load_imgs(img_path)

        seg_path = '%s/%s_maskresized.png' % (data_dir, key)
        seg = self.load_segs(seg_path)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path):
        img = cv2.imread(img_path)
        img = Image.fromarray(img)

        return self.transform(img)

    def load_segs(self, seg_path):
        img = Image.open(seg_path).convert('1')

        # return self.transform_seg(img)
        return (self.transform_seg(img) > 0.5).long()

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item

class CarDatasetCenterCrop(Dataset):
    def __init__(
            self,
            root_dir,
            size,
            data_split=0,
            use_flip=False,
    ):
        super().__init__()
        self.data_split = data_split
        self.use_flip = use_flip

        self._aspect_ratio = 1.0
        self._crop = 'center'

        self.transform = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3)])

        self.transform_seg = transforms.Compose([
            transforms.Resize((int(size), int(size))),
            transforms.ToTensor()])

        self.ROOT_DIR = root_dir
        self.IM_SIZE = size

        self.file_meta = self.collect_meta()

    def __len__(self):
        return len(self.file_meta)

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item

    def collect_meta(self):
        sel_indices_tr = np.load('{}/data_mrcnn_train_select.npy'.format(self.ROOT_DIR))
        sel_indices_te = np.load('{}/data_mrcnn_test_select.npy'.format(self.ROOT_DIR))

        if self.data_split == 0: # training split
            filenames = ['data_mrcnn/train/orig/{}'.format(token) for token in sel_indices_tr]
        elif self.data_split == 2: # testing split
            filenames = ['data_mrcnn/test/orig/{}'.format(token) for token in sel_indices_te]
        elif self.data_split == -1: # all dataset
            filenames = ['data_mrcnn/train/orig/{}'.format(token) for token in sel_indices_tr] \
                        + ['data_mrcnn/test/orig/{}'.format(token) for token in sel_indices_te]
        return filenames

    def load_item(self, index):
        key = self.file_meta[index]

        data_dir = self.ROOT_DIR

        img_path = '%s/%s_orig.png' % (data_dir, key)
        img = self.load_imgs(img_path)

        seg_path = '%s/%s_maskorig.png' % (data_dir, key)
        seg = self.load_segs(seg_path)

        if self.use_flip and np.random.uniform() > 0.5:
            img = torch.flip(img, dims=[-1])
            seg = torch.flip(seg, dims=[-1])

        if seg.ndim > 2:
            seg = seg[0]

        # return img, seg, index
        return img, seg

    def load_imgs(self, img_path):
        img = cv2.imread(img_path)
        img = Image.fromarray(img)

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        return self.transform(image)

    def load_segs(self, seg_path):
        img = Image.open(seg_path).convert('1')

        image = img
        i, j, h, w = self._get_crop_params(image) # top, left, height, width
        image = torchvision.transforms.functional.resized_crop(image, i, j, h, w, self.IM_SIZE)

        # return self.transform_seg(img)
        return (self.transform_seg(image) > 0.5).long()

    def _get_crop_params(self, x):
        width, height = x.size

        if self._crop == 'center':
            #
            i, j = 0, 0
            if height > width * self._aspect_ratio: # height is longer than expected
                i = int((height - width * self._aspect_ratio) // 2)
                height = int(width * self._aspect_ratio)

            elif height < width * self._aspect_ratio:
                j = int((width - height / self._aspect_ratio) // 2)
                width = int(height / self._aspect_ratio)

        elif self._crop == 'random':
            scale = np.random.uniform(*self._resize_scale)
            ww, hh = int(scale * width), int(scale * height) # Scale
            ww = min(ww, hh / self._aspect_ratio) # Crop to aspect_ratio
            hh = min(hh, ww * self._aspect_ratio)
            i = int(np.random.uniform(0, height - hh))
            j = int(np.random.uniform(0, width -ww))
            height, width = hh, ww

        else:
            raise NotImplementedError

        return i, j, height, width

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item


class SSSDataset(Dataset):
    def __init__(
            self,
    ):
        super().__init__()
        self.data = torch.load('/lustre/cniel/onr/sss_masks.pt')


    def __len__(self):
        return len(self.data['images'])

    def __getitem__(self, index):
        try:
            item = self.load_item(index)
        except:
            print('loading error: sample # {}'.format(index))
            item = self.load_item(0)

        return item


    def load_item(self, index):
        return self.data['images'][index], self.data['masks'][index]

    def create_iterator(self, batch_size):
        while True:
            sample_loader = DataLoader(
                dataset=self,
                batch_size=batch_size,
                drop_last=True,
                shuffle=False
            )

            for item in sample_loader:
                yield item



#----------------------------------------------------------------------------

class UNetBlock(torch.nn.Module):
    def __init__(self, in_channels, out_channels, up=False, down=False, bilinear=True):
        super().__init__()
        self.up_scale = None
        self.down_scale = None
        assert not (up and down)
        if up:
            self.up_scale = torch.nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True) if bilinear else \
                            torch.nn.ConvTranspose2d(in_channels // 2, in_channels // 2, kernel_size=2, stride=2)
        if down:
            self.down_scale = torch.nn.MaxPool2d(2)

        self.conv1 = torch.nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.bn1 = torch.nn.BatchNorm2d(out_channels)
        self.conv2 = torch.nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = torch.nn.BatchNorm2d(out_channels)

    def forward(self, x1, x2=None):
        x = x1

        if self.up_scale is not None:
            assert x2 is not None
            x1 = self.up_scale(x1)
            # pad x1 if the size does not match the size of x2
            dh = x2.size(2) - x1.size(2)
            dw = x2.size(3) - x1.size(3)
            x1 = torch.nn.functional.pad(x1, [dw // 2, dw - dw // 2, dh // 2, dh - dh // 2])
            x = torch.cat([x2, x1], dim=1)

        if self.down_scale is not None:
            x = self.down_scale(x1)

        x = torch.nn.functional.relu_(self.bn1(self.conv1(x)))
        x = torch.nn.functional.relu_(self.bn2(self.conv2(x)))
        return x

class UNet(torch.nn.Module):
    def __init__(self, input_channels=1, output_channels=1, bilinear=True, scale=64):
        super(UNet, self).__init__()
        self.scale = scale
        if scale == 64:
            self.input_channels = input_channels
            self.output_channels = output_channels
            self.bilinear = bilinear

            self.inc = UNetBlock(self.input_channels, 64)

            self.down1 = UNetBlock(64, 128, down=True)
            self.down2 = UNetBlock(128, 256, down=True)
            self.down3 = UNetBlock(256, 512, down=True)
            self.down4 = UNetBlock(512, 512, down=True)

            self.up1 = UNetBlock(1024, 256, up=True, bilinear=bilinear)
            self.up2 = UNetBlock(512, 128, up=True, bilinear=bilinear)
            self.up3 = UNetBlock(256, 64, up=True, bilinear=bilinear)
            self.up4 = UNetBlock(128, 64, up=True, bilinear=bilinear)

            self.outc = torch.nn.Conv2d(64, self.output_channels, kernel_size=1)

        elif scale == 128:
            self.input_channels = input_channels
            self.output_channels = output_channels
            self.bilinear = bilinear

            self.inc = UNetBlock(self.input_channels, 64)

            self.down1 = UNetBlock(64, 128, down=True)
            self.down2 = UNetBlock(128, 256, down=True)
            self.down3 = UNetBlock(256, 512, down=True)
            self.down4 = UNetBlock(512, 512, down=True)
            self.down5 = UNetBlock(512, 512, down=True)

            self.up0 = UNetBlock(1024, 512, up=True, bilinear=bilinear)
            self.up1 = UNetBlock(1024, 256, up=True, bilinear=bilinear)
            self.up2 = UNetBlock(512, 128, up=True, bilinear=bilinear)
            self.up3 = UNetBlock(256, 64, up=True, bilinear=bilinear)
            self.up4 = UNetBlock(128, 64, up=True, bilinear=bilinear)

            self.outc = torch.nn.Conv2d(64, self.output_channels, kernel_size=1)

        else:
            raise AssertionError()



    def forward(self, x):
        if self.scale == 64:
            x1 = self.inc(x)
            x2 = self.down1(x1)
            x3 = self.down2(x2)
            x4 = self.down3(x3)
            x5 = self.down4(x4)
            x = self.up1(x5, x4)
            x = self.up2(x, x3)
            x = self.up3(x, x2)
            x = self.up4(x, x1)
            logits = self.outc(x)

        elif self.scale == 128:
            x1 = self.inc(x)  # 64
            x2 = self.down1(x1)  # 128
            x3 = self.down2(x2)  # 256
            x4 = self.down3(x3)  # 512
            x5 = self.down4(x4)  # 512
            x6 = self.down5(x5)  # 512

            x = self.up0(x6, x5)
            x = self.up1(x, x5)
            x = self.up2(x, x3)
            x = self.up3(x, x2)
            x = self.up4(x, x1)
            logits = self.outc(x)

        else:
            raise AssertionError()

        return logits

#----------------------------------------------------------------------------

def calc_metrics(metrics, model, testloader, device):
    start_time = time.time()

    nC = model.output_channels
    C = np.zeros((nC, nC), dtype=np.int64) # Confusion matrix: [Pred x GT]

    iou_s = 0
    dice_s = 0
    cnt = 0

    ari = []
    msc = []

    model.eval().requires_grad_(False)
    for img, gt_mask in testloader:
        pred_logits = model(img.to(device))
        pred_mask = pred_logits.max(dim=1)[1]

        bs = pred_mask.shape[0]
        pred = pred_mask
        gt = gt_mask.to(device)

        pred_mask = pred_mask.cpu().numpy()
        gt_mask = gt_mask.cpu().numpy()
        C += np.bincount(
            nC * pred_mask.reshape(-1) + gt_mask.reshape(-1), # the value is one of [0, 1, 2, 3] which suggests tn, fn, fp, tp
            minlength=nC ** 2).reshape(nC, nC) # reshape to [Pred x GT]

        # metric code used in DRC
        iou = (pred * gt).view(bs, -1).sum(dim=-1) / \
              ((pred + gt) > 0).view(bs, -1).sum(dim=-1)

        dice = 2 * (pred * gt).view(bs, -1).sum(dim=-1) / \
               (pred.view(bs, -1).sum(dim=-1) + gt.view(bs, -1).sum(dim=-1))

        iou_s += iou.sum().item()
        dice_s += dice.sum().item()
        cnt += bs

        # ari
        if 'ARI' in metrics:
            ari.append(adjusted_rand_score(pred_mask.flatten(), gt_mask.flatten()))

        # msc
        if 'MSC' in metrics:
            msc_, _ = np_average_segcover(gt_mask[:, None], pred_mask[:, None])
            msc.append(msc_)


    model.train().requires_grad_(True)

    assert all([metric in ['pACC', 'IoU', 'mIoU', 'DRC_IoU', 'DRC_DICE', 'ARI', 'MSC'] for metric in metrics])

    results = {}
    C = C.astype(np.float64)
    if 'pACC' in metrics:
        # pACC = (tn + tp) / (tn + fn + fp + tp)
        results['pACC'] = C.diagonal().sum() / C.sum()
    if 'IoU' in metrics or 'mIoU' in metrics:
        # IoU = tp / (tp + fn + fp)
        union = C.sum(axis=1) + C.sum(axis=0) - C.diagonal()
        union[union == 0] = 1e-8
        iou_vals = C.diagonal() / union # (nC,)
        if 'IoU' in metrics:
            results['IoU'] = iou_vals[1]
        if 'mIoU' in metrics:
            results['mIoU'] = iou_vals.mean()

    results['DRC_IoU'] = iou_s / cnt
    results['DRC_DICE'] = dice_s / cnt

    if 'ARI' in metrics:
        # Compute the ARI
        results['ARI'] = np.mean(ari)

    if 'MSC' in metrics:
        # In our case mIoU is nearly equivalent to MSC, but we still re-compute it for sure
        results['MSC'] = np.mean(msc)


    total_time = time.time() - start_time

    return dict(
        results         = results,
        metrics         = metrics,
        total_time      = total_time,
        total_time_str  = dnnlib.util.format_time(total_time),
    )

#----------------------------------------------------------------------------

def report_metrics(result_dict, run_dir=None, snapshot_pth=None, split='val'):
    if run_dir is not None and snapshot_pth is not None:
        snapshot_pth = os.path.relpath(snapshot_pth, run_dir)

    jsonl_line = json.dumps(dict(result_dict, snapshot_pth=snapshot_pth, timestamp=time.time()))
    print(jsonl_line)
    if run_dir is not None and os.path.isdir(run_dir):
        with open(os.path.join(run_dir, f'metric-{split}.jsonl'), 'at') as f:
            f.write(jsonl_line + '\n')

#----------------------------------------------------------------------------

def resume(to_resume, snapshot_pth):
    print(f'Resume from {snapshot_pth}...')

    snapshot_data = torch.load(snapshot_pth)
    for name, module in to_resume.items():
        module.load_state_dict(snapshot_data[name])
    return snapshot_data.get('cur_iter', 0)

#----------------------------------------------------------------------------

def save_seg_grid(results, fname, grid_size):
    gw, gh = grid_size
    N = gw * gh

    def pre_reshape(x):
        lo, hi = (-1, 1) if x.ndim > 3 else (0, 1)
        x = np.asarray(x, dtype=np.float32)
        x = (x - lo) * (255 / (hi - lo))
        if x.ndim == 3:
            x = np.tile(x[:, None], (1, 3, 1, 1))
        x = np.rint(x).clip(0, 255).astype(np.uint8)
        return x

    results = [pre_reshape(torch.cat(x, dim=0).cpu()[:N]) for x in results]
    grid_im = np.stack(results, axis=3) # (N, C, H, 3, W)
    grid_im = grid_im.reshape(gh, gw, *grid_im.shape[1:]) # (gh, gw, C, H, 3, W)
    grid_im = grid_im.transpose(0, 3, 1, 4, 5, 2) # (gh, H, gw, 3, W, C)
    out_h = np.prod(grid_im.shape[:2])
    out_w = np.prod(grid_im.shape[2:5])
    grid_im = grid_im.reshape(out_h, out_w, -1)
    Image.fromarray(grid_im, 'RGB').save(fname)

#----------------------------------------------------------------------------

def augment_data(op, img, mask):
    if op is None:
        return img, mask

    to_hard_mask = False
    if mask.dim() == 3:
        # Convert to one-hot
        mask = F.one_hot(mask).permute(0, 3, 1, 2).to(torch.float32)
        to_hard_mask = True

    # Transform img
    img, params = op(img, is_mask=False)
    mask, _ = op(mask, params, is_mask=True)

    if to_hard_mask:
        mask = mask.max(dim=1).indices

    return img, mask

#----------------------------------------------------------------------------

def segmentation(
    run_dir,
    syn_data,
    real_data,
    #
    aug                     = None,
    #
    resume_from             = None,
    test_only               = False,
    test_all                = False,
    metrics                 = ['pACC', 'IoU', 'mIoU', 'DRC_IoU', 'DRC_DICE'],
    #
    batch                   = 64,
    total_iter              = 6000,  # 12000
    lr                      = 0.001,
    lr_steps                = 4000,  # 8000
    lr_decay                = 0.2,
    #
    niter_per_tick          = 20,
    network_snapshot_ticks  = 50,
    image_snapshot_ticks    = 50,
    #
    random_seed             = 0,
    cudnn_benchmark         = True,
    scale                   = 128,
    crop_ratio              = 1.0,
    extra_center_crop       = False,
):
    # Initialize.
    start_time = time.time()
    device = torch.device('cuda')
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)
    torch.backends.cudnn.benchmark = cudnn_benchmark  # Improves training speed.

    # Load training, validation (synthetic data), and test set (real data).
    assert os.path.exists(os.path.join(syn_data, 'img'))
    assert os.path.exists(os.path.join(syn_data, 'mask'))
    # assert real_data in ['cub', 'flowers', 'lsun_car']

    whole_dataset = SynSegDataset(syn_data, size=scale, crop_ratio=crop_ratio)
    num_samples = len(whole_dataset)
    del whole_dataset

    split_num = int(num_samples * 0.9)

    trainset = SynSegDataset(syn_data, range(split_num), size=scale, crop_ratio=crop_ratio)
    trainset_sampler = misc.InfiniteSampler(dataset=trainset, seed=random_seed)
    data_loader_kwargs = dict(pin_memory=True, num_workers=3, prefetch_factor=2)
    trainset_iterator = iter(torch.utils.data.DataLoader(dataset=trainset, sampler=trainset_sampler,
                                                         batch_size=batch, **data_loader_kwargs))
    valset = SynSegDataset(syn_data, range(split_num, num_samples), size=scale, crop_ratio=crop_ratio)
    valset_loader = torch.utils.data.DataLoader(dataset=valset, batch_size=batch, drop_last=False, **data_loader_kwargs)

    # prepare the RealDataset
    assert real_data in ['cub', 'dog', 'car','sss']
    if real_data == 'cub':
        testset = CubDataset(
            root_dir='../../../datasets_local/DRC_processed/birds',
            size=scale,
            data_split=2,
            use_flip=False,
        )
    elif real_data == 'dog':
        testset = DogDataset(
            root_dir='../../../datasets_local/DRC_processed/dogs',
            size=scale,
            data_split=2,
            use_flip=False,
        )
    elif real_data == 'car':
        testset = CarDataset(
            root_dir='../../../datasets_local/DRC_processed/cars',
            size=scale,
            data_split=2,
            use_flip=False,
        )
    elif real_data == 'sss':
        testset = SSSDataset()
    else:
        raise NotImplementedError()

    if extra_center_crop:
        if real_data == 'cub':
            testset_center_crop = CubDatasetCenterCrop(
                root_dir='../../../datasets_local/DRC_processed/birds',
                size=scale,
                data_split=2,
                use_flip=False,
            )
            raise NotImplementedError('Should use the orig image. Now still use resized!')

        elif real_data == 'dog':
            testset_center_crop = DogDatasetCenterCrop(
                root_dir='../../../datasets_local/DRC_processed/dogs',
                size=scale,
                data_split=2,
                use_flip=False,
            )
            raise NotImplementedError('Should use the orig image. Now still use resized!')
        elif real_data == 'car':
            testset_center_crop = CarDatasetCenterCrop(
                root_dir='../../../datasets_local/DRC_processed/cars',
                size=scale,
                data_split=2,
                use_flip=False,
            )
            raise NotImplementedError('Lacks of the mask of original image!')
        else:
            raise NotImplementedError()


    print('Num of test images:  ', len(testset))
    testset_loader = torch.utils.data.DataLoader(dataset=testset, batch_size=batch, drop_last=False, **data_loader_kwargs)

    if extra_center_crop:
        print('center crop dataset is not implemented!')
        testset_center_crop_loader = torch.utils.data.DataLoader(dataset=None, batch_size=batch, drop_last=False, **data_loader_kwargs)

    # Construct networks.
    model = UNet(scale=64).to(device).eval().requires_grad_(False)
    # Construct augmentation.
    # augmentation from ELGANv2.0
    if aug is not None:
        aug_kwargs = {
            'geom': dict(xflip=0.5, scale=0.5, rotate=0.5, xfrac=0.5),
            'color': dict(brightness=0.5, contrast=0.5, lumaflip=0.5, hue=0.5, saturation=0.5),
            'gc': dict(xflip=0.5, scale=0.5, rotate=0.5, xfrac=0.5,
                       brightness=0.5, contrast=0.5, lumaflip=0.5, hue=0.5, saturation=0.5),
        }[aug]
        augment = dnnlib.util.construct_class_by_name(class_name='training.seg_augment.AugmentPipe', **aug_kwargs)
    else:
        augment = None

    # Print network summary tables.
    misc.print_module_summary(model, [torch.empty([batch, 1, trainset.resolution, trainset.resolution], device=device)])

    # Setup training phases.
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(opt, lr_steps, lr_decay)

    # Export sample images.
    print('Exporting segmentation visualization...')
    grid_size = (7, 32)
    grid_img, grid_gt = zip(*[next(trainset_iterator) for _ in range((np.prod(grid_size) - 1) // batch + 1)])
    grid_img = [x.to(device) for x in grid_img]
    grid_pred = [model(img.to(device)).max(dim=1)[1] for img in grid_img]
    save_seg_grid([grid_img, grid_pred, grid_gt], os.path.join(run_dir, 'seg-train_init.png'), grid_size=grid_size)

    # Visualize augmentation
    grid_augx, grid_augy = zip(*[augment_data(augment, img.to(device), mask.to(device)) for img, mask in zip(grid_img, grid_gt)])
    save_seg_grid([grid_img, grid_gt, grid_augx, grid_augy], os.path.join(run_dir, 'augment.png'), grid_size=grid_size)

    del grid_pred, grid_augx, grid_augy
    torch.cuda.empty_cache()

    # Initialize logs.
    print('Initializing logs...')
    stats_collector = training_stats.Collector(regex='.*')
    stats_metrics = dict()
    stats_tfevents = None
    stats_jsonl = open(os.path.join(run_dir, 'stats.jsonl'), 'wt')
    try:
        import torch.utils.tensorboard as tensorboard
        stats_tfevents = tensorboard.SummaryWriter(run_dir)
    except ImportError as err:
        print('Skipping tfevents export:', err)

    # Resume from existing pth.
    cur_iter = 0
    if resume_from is not None:
        assert os.path.exists(resume_from) and os.path.isdir(resume_from)
        from glob import glob
        resume_pth = sorted(glob(os.path.join(resume_from, '*.pth')))[-1]
        cur_iter = resume(dict(model=model, opt=opt, lr_sch=lr_scheduler), resume_pth)

    # Train
    print(f'Training for {total_iter} iterations...')
    print(f'Start from {cur_iter} iterations ...')
    print()
    cur_tick = 0
    tick_start_iter = cur_iter
    tick_start_time = time.time()
    maintenance_time = tick_start_time - start_time
    do_train = not test_only
    if do_train:
        model.train().requires_grad_(True)
    while do_train:
        # Fetch data
        img, target = next(trainset_iterator)
        img = img.to(device)
        target = target.to(device)

        # Forward and backward
        opt.zero_grad()
        img, target = augment_data(augment, img, target)
        pred = model(img)
        loss = torch.nn.functional.cross_entropy(pred, target)
        training_stats.report('Loss/loss', loss)
        loss.backward()
        opt.step()

        # Update state.
        cur_iter += 1

        # Perform maintenance tasks once per tick.
        done = (cur_iter >= total_iter)
        if (not done) and (cur_iter != 0) and (cur_iter < tick_start_iter + niter_per_tick):
            continue

        # Print status line, accumulating the same information in stats_collector.
        tick_end_time = time.time()
        fields = []
        fields += [f"tick {training_stats.report0('Progress/tick', cur_tick):<5d}"]
        fields += [f"niter {training_stats.report0('Progress/niter', cur_iter):<8d}"]
        fields += [f"time {dnnlib.util.format_time(training_stats.report0('Timing/total_sec', tick_end_time - start_time)):<12s}"]
        fields += [f"sec/tick {training_stats.report0('Timing/sec_per_tick', tick_end_time - tick_start_time):<7.1f}"]
        fields += [f"sec/iter {training_stats.report0('Timing/sec_per_iter', (tick_end_time - tick_start_time) / (cur_iter - tick_start_iter)):<7.2f}"]
        fields += [f"maintenance {training_stats.report0('Timing/maintenance_sec', maintenance_time):<6.1f}"]
        fields += [f"cpumem {training_stats.report0('Resources/cpu_mem_gb', psutil.Process(os.getpid()).memory_info().rss / 2 ** 30):<6.2f}"]
        fields += [f"gpumem {training_stats.report0('Resources/peak_gpu_mem_gb', torch.cuda.max_memory_allocated(device) / 2 ** 30):<6.2f}"]
        torch.cuda.reset_peak_memory_stats()
        training_stats.report0('Timing/total_hours', (tick_end_time - start_time) / (60 * 60))
        training_stats.report0('Timing/total_days', (tick_end_time - start_time) / (24 * 60 * 60))
        print(' '.join(fields))

        # Save segmentation snapshot.
        if (image_snapshot_ticks is not None) and (done or cur_tick % image_snapshot_ticks == 0):
            model.eval().requires_grad_(False)
            grid_pred = [model(img.to(device)).max(dim=1)[1] for img in grid_img]
            save_seg_grid([grid_img, grid_pred, grid_gt], os.path.join(run_dir, f'seg-train_{cur_iter:06d}.png'),
                          grid_size=grid_size)
            del grid_pred
            model.train().requires_grad_(True)

        # Save network snapshot.
        snapshot_data = None
        snapshot_pth = None
        if (network_snapshot_ticks is not None) and (done or cur_tick % network_snapshot_ticks == 0):
            # snapshot_data = dict(training_set_kwargs=dict(training_set_kwargs))
            snapshot_data = {name: module.state_dict() for name, module in [('model', model), ('opt', opt), ('lr_sch', lr_scheduler)]}
            snapshot_data['cur_iter'] = cur_iter
            snapshot_pth = os.path.join(run_dir, f'network-snapshot-{cur_iter:06d}.pth')
            torch.save(snapshot_data, snapshot_pth)

        # Evaluate metrics.
        if (snapshot_data is not None) and len(metrics) > 0:
            print('Evaluating metrics...')
            result_dict = calc_metrics(metrics=metrics, model=model, testloader=valset_loader, device=device)
            report_metrics(result_dict, run_dir=run_dir, snapshot_pth=snapshot_pth, split='val')
            stats_metrics.update(result_dict["results"])
        del snapshot_data  # conserve memory

        # Collect statistics.
        stats_collector.update()
        stats_dict = stats_collector.as_dict()

        # Update logs.
        timestamp = time.time()
        if stats_jsonl is not None:
            fields = dict(stats_dict, timestamp=timestamp)
            stats_jsonl.write(json.dumps(fields) + '\n')
            stats_jsonl.flush()
        if stats_tfevents is not None:
            global_step = cur_iter
            walltime = timestamp - start_time
            for name, value in stats_dict.items():
                stats_tfevents.add_scalar(name, value.mean, global_step=global_step, walltime=walltime)
            for name, value in stats_metrics.items():
                stats_tfevents.add_scalar(f'Metrics/{name}', value, global_step=global_step, walltime=walltime)
            stats_tfevents.flush()

        # Update state.
        cur_tick += 1
        tick_start_iter = cur_iter
        tick_start_time = time.time()
        maintenance_time = tick_start_time - tick_end_time

        if done:
            break

    if test_only:
        print('Traininig is skipped')

    if test_all:
        # test all the intermediate ckpt
        print('test all the intermediate ckpt')
        metric_jsonl = os.path.join(run_dir, 'metric-val.jsonl')
        with open(metric_jsonl, 'rt') as f:
            metric_results = [json.loads(line) for line in f]
        snap_list = sorted([entry['snapshot_pth'] for entry in metric_results])
        ckpt_path_list = [os.path.join(run_dir, snap) for snap in snap_list]
    else:
        # Resume from the best (maximum IoU)
        print('Conclude training, resume the snapshot of best validation performance ...')
        metric_jsonl = os.path.join(run_dir, 'metric-val.jsonl')
        with open(metric_jsonl, 'rt') as f:
            metric_results = [json.loads(line) for line in f]
        maxiou_idx = np.asarray([entry['results']['IoU'] for entry in metric_results]).argmax()
        ckpt_path_list = [os.path.join(run_dir, metric_results[maxiou_idx]['snapshot_pth'])]

    # Evaluation of testset
    # Evaluate and report on test split.
    print('\n\n Evaluating on resized dataset (default DRC processed)!\n')
    for ckpt_path in ckpt_path_list:
        resume(dict(model=model, opt=opt, lr_sch=lr_scheduler), ckpt_path)
        result_dict = calc_metrics(metrics=metrics, model=model, testloader=testset_loader, device=device)
        if test_all:
            report_metrics(result_dict, run_dir=run_dir, snapshot_pth=ckpt_path, split='test_all')
        else:
            report_metrics(result_dict, run_dir=run_dir, snapshot_pth=metric_results[maxiou_idx]['snapshot_pth'], split='test')

        # Save segmentation snapshot.
        testset_iterator = iter(testset_loader)
        grid_img, grid_gt = zip(*[next(testset_iterator) for _ in range((np.prod(grid_size) - 1) // batch + 1)])
        grid_img = [x.to(device) for x in grid_img]
        model.eval().requires_grad_(False)
        grid_pred = [model(img.to(device)).max(dim=1)[1] for img in grid_img]
        model.train().requires_grad_(True)
        save_seg_grid([grid_img, grid_pred, grid_gt], os.path.join(run_dir, 'seg-test.png'), grid_size=grid_size)


    if extra_center_crop:
        # Evaluation of testset_center_crop
        # Evaluate and report on test split.
        print('\n\n Evaluating on center cropped DRC split!\n')
        result_dict = calc_metrics(metrics=metrics, model=model, testloader=testset_center_crop_loader, device=device)
        report_metrics(result_dict, run_dir=run_dir, snapshot_pth=metric_results[maxiou_idx]['snapshot_pth'], split='test-center-crop')

        # Save segmentation snapshot.
        testset_iterator = iter(testset_center_crop_loader)
        grid_img, grid_gt = zip(*[next(testset_iterator) for _ in range((np.prod(grid_size) - 1) // batch + 1)])
        grid_img = [x.to(device) for x in grid_img]
        model.eval().requires_grad_(False)
        grid_pred = [model(img.to(device)).max(dim=1)[1] for img in grid_img]
        model.train().requires_grad_(True)
        save_seg_grid([grid_img, grid_pred, grid_gt], os.path.join(run_dir, 'seg-test-center-crop.png'), grid_size=grid_size)

    print()
    print('Done')

#----------------------------------------------------------------------------

@click.command()
@click.pass_context
# @click.option('--outdir', help='Where to save the results', required=True, metavar='DIR')
# @click.option('--generator', help='generator pkl directory')
@click.option('--syn-data', help='Where to load synthetic data', metavar='DIR')
@click.option('--real-data', help='Which dataset to evaluate on', required=True, type=str)

@click.option('--aug', help='Which augmentation to be used', type=str)
@click.option('--extra-center-crop', help='Extra evaluation on center cropped DRC split', type=bool, metavar='BOOL')

@click.option('--resume-from', help='Where to resume')
@click.option('--test-only', help='Only test the model', type=bool, metavar='BOOL')
@click.option('--test-all', help='Test all the intermediate ckpt', type=bool, metavar='BOOL')
@click.option('--scale', help='Test img scale [default: 128]', type=int, metavar='INT')
@click.option('--crop-ratio', help='CenterCrop ratio of synthetic img', type=float, metavar='FLOAT')
@click.option('--seed', help='Random seed [default: 0]', type=int, metavar='INT')
@click.option('-n', '--dry-run', help='Print training options and exit', is_flag=True)
def main(ctx, dry_run, **config_kwargs):
    # Generate dataset if only the generator is provided

    # if config_kwargs['syn_data'] is None:
    #     generator_file = config_kwargs.pop('generator', None)
    #     assert generator_file is not None
    #     generate_fn(generator_file, resolution=128)
    #     config_kwargs['syn_data'] = os.path.join(generator_file, 'synthetic_data')

    # Setup training options.
    args = dnnlib.util.EasyDict({k: v for k, v in config_kwargs.items() if v is not None})
    # real_dir = args.real_data.strip('/').split('/')[-1]
    # syn_dir = args.syn_data.strip('/').split('/')[-2]
    # run_desc = f'{real_dir}-{syn_dir}'

    # Pick output directory.
    outdir = args.syn_data
    prev_run_dirs = []
    if os.path.isdir(outdir):
        prev_run_dirs = [x for x in os.listdir(outdir) if os.path.isdir(os.path.join(outdir, x))]
    prev_run_ids = [re.match(r'^\d+', x) for x in prev_run_dirs]
    prev_run_ids = [int(x.group()) for x in prev_run_ids if x is not None]
    cur_run_id = max(prev_run_ids, default=-1) + 1
    # args.run_dir = os.path.join(outdir, f'{cur_run_id:05d}-{run_desc}')
    if 'aug' in args:
        args.run_dir = os.path.join(outdir, f'{cur_run_id:05d}-DRCreal-{args.aug}_aug')
    else:
        args.run_dir = os.path.join(outdir, f'{cur_run_id:05d}-DRCreal')

    if 'scale' in args:
        args.run_dir = args.run_dir + '-' + str(args.scale)

    if 'crop_ratio' in args:
        args.run_dir = args.run_dir + '-CropRatio' + str(args.crop_ratio)

    assert not os.path.exists(args.run_dir)

    if config_kwargs['resume_from'] is not None:
        args.run_dir = config_kwargs['resume_from']

    # Print options.
    print()
    print('Training options:')
    print(json.dumps(args, indent=2))
    print()
    print(f'Output directory:   {args.run_dir}')
    print()

    # Dry run?
    if dry_run:
        print('Dry run; exiting.')
        return

    # Create output directory.
    print('Creating output directory...')
    os.makedirs(args.run_dir, exist_ok=True)
    with open(os.path.join(args.run_dir, 'training_options.json'), 'wt') as f:
        json.dump(args, f, indent=2)

    # Launch processes.
    dnnlib.util.Logger(file_name=os.path.join(args.run_dir, 'log.txt'), file_mode='a', should_flush=True)
    segmentation(**args)

#----------------------------------------------------------------------------

if __name__ == "__main__":
    main() # pylint: disable=no-value-for-parameter

#----------------------------------------------------------------------------
