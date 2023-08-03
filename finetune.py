# TODO 
# what happens with qc=0? is num filters or inputs changed?

# coding: utf-8
import os
os.environ["OMP_NUM_THREADS"] = '1'

import torch
from torch.cuda import amp

from tqdm import tqdm
import numpy as np
import yaml
import copy
from pathlib import Path

from utils_yolo.test import test
from utils_kse import models

from utils_yolo.general import check_dataset, init_seeds, fitness, increment_path
from utils_yolo.loss import ComputeLossOTA
from utils_yolo.finetune import *


validate_before_training = False

# KSE params
G = 3
T = 1

# files / dirs
compressed_model = './data/compressed/G{}T{}-tiny.pth'.format(G, T)
compressed_model = './data/compressed/G3T0-tiny.pth'
save_dir = Path(increment_path(Path('tmp/training'), exist_ok=False))
wdir = save_dir / 'weights'
last = wdir / 'last.pth'
best = wdir / 'best.pth'
results_file = save_dir / 'results.txt'
loss_file = save_dir / 'losses.txt'

data = './data/coco.vast.yaml'
hyp = './data/hyp.scratch.tiny.yaml'
yolo_struct = './data/yolov7_tiny_struct.yaml'

# training params
epochs = 20
batch_size = 8
num_workers = 4
img_size = [640, 640]
nbs = 64 # nominal batch size
accumulate = max(round(nbs / batch_size), 1)


if __name__ == "__main__":
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    cuda = device.type != 'cpu'

    os.makedirs(wdir, exist_ok=True) # inside __main__!!!
    init_seeds(1)
    
    # check data
    with open(hyp) as f:
        hyp = yaml.load(f, Loader=yaml.SafeLoader)
    with open(data) as f:
        data_dict = yaml.load(f, Loader=yaml.SafeLoader)
    with open(yolo_struct) as f:
        yolo_struct = yaml.load(f, Loader=yaml.SafeLoader)
    check_dataset(data_dict)

    # load model
    nc = int(data_dict['nc'])   # number of classes
    model = load_model(yolo_struct, nc, compressed_model, G, T, device)
    
    # load data
    # data_dict['train'] = data_dict['val'] # for testing (reduces load time)
    imgsz_test, dataloader, dataset, testloader, hyp, model = load_data(model, img_size, data_dict, batch_size, hyp, num_workers, device)
    nb = len(dataloader)        # number of batches

    # optimizer
    optimizer, scheduler = create_optimizer(model, hyp)

    # scaler + loss
    scaler = amp.GradScaler(enabled=cuda)
    compute_loss = ComputeLossOTA(model)  # init loss class

    # run validation before training
    maps = np.zeros(nc) # mAP per class
    results = (0, 0, 0, 0, 0, 0, 0)  # P, R, mAP@.5, mAP@.5-.95, val_loss(box, obj, cls)
    best_fitness = 0
    if validate_before_training:
        results = test(
            data_dict,
            batch_size=batch_size,
            imgsz=imgsz_test,
            model=model,
            dataloader=testloader,
            save_dir=save_dir,
            # compute_loss=compute_loss,
            plots=False,
            is_coco=True,
            save_json=False,
            iou_thres=0.65
        )[0]
        best_fitness = fitness(np.array(results).reshape(1, -1))
        print('Starting fitness:', best_fitness)

    # train loop
    l_file = open(loss_file, 'w')
    l_file.write(('{:>10s}' * 6 + '\n').format('epoch', 'gpu_mem', 'box', 'obj', 'cls', 'total'))
    with open(results_file, 'a') as r_file:
            r_file.write(f"{G=}, {T=}, {epochs=}, {batch_size=}, {hyp['lr0']=}, {hyp['lrf']=}\n")
            r_file.write(('{:>10s}'*11 + '\n').format('epoch', 'mp', 'mr', 'mAP50', 'mAP', 'box', 'obj', 'cls', 'mAP[0]', 'fitness', 'new_lr'))
            

    print(('{:>10s}' * 7).format('Epoch', 'gpu_mem', 'box', 'obj', 'cls', 'total', 'labels'))
    for epoch in range(epochs):
        model.train()

        mloss = torch.zeros(4, device=device)
        optimizer.zero_grad()

        pbar = tqdm(enumerate(dataloader), total=nb)
        ix = 0
        for i, (imgs, targets, paths, _) in pbar:
            ni = i + nb * epoch
            imgs = imgs.to(device, non_blocking=True).float() / 255.0  # uint8 to float32, 0-255 to 0.0-1.0
            
            # forward pass
            with amp.autocast(enabled=cuda):
                pred = model(imgs)
                loss, loss_items = compute_loss(pred, targets.to(device), imgs)
            # backprop
            scaler.scale(loss).backward()

            # optimize (every nominal batch size)
            if ni % accumulate == 0:
                ix+=1
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

            # print
            rate = pbar.format_dict['rate']
            remaining = (pbar.total - pbar.n) / rate / 60 if rate and pbar.total else 0
            mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
            mem = '%.3gG' % (torch.cuda.memory_reserved() / 1E9 if torch.cuda.is_available() else 0)  # (GB)
            s = ('%10s' * 2 + '%10.4g' * 5) % (
                '%g/%g' % (epoch, epochs - 1), mem, *mloss, targets.shape[0])
            pbar.set_description(s)

            # save losses each nominal batch
            if ni % accumulate == 0:
                s = ('%10s' * 2 + '%10.4g' * 4 + '\n') % (
                '%g/%g' % (epoch, epochs - 1), mem, *loss_items)
                l_file.write(s)

            # if ix == 200:
            #     break
            # end batch

        # update learning rate
        scheduler.step()
        new_lr = optimizer.param_groups[0]["lr"]
        print('New learning rate: {:.2e}'.format(new_lr))

        # validation
        results, maps, times = test(
            data_dict,
            batch_size=batch_size * 2,
            imgsz=imgsz_test,
            model=model,
            dataloader=testloader,
            save_dir=save_dir,
            compute_loss=compute_loss,
            is_coco=True,
            plots=False,
            iou_thres=0.65
        )

        # update best fitness
        fi = fitness(np.array(results).reshape(1, -1))  # weighted combination of [P, R, mAP@.5, mAP@.5-.95]
        if fi > best_fitness:
            best_fitness = fi
            print('Found higher fitness:', best_fitness)

        # write results to file
        with open(results_file, 'a') as f:
            f.write(('{:7d}/{:2d}' + '{:10.4g}'*9 + '{:10.2e}\n').format(epoch, epochs-1, *results, maps[0], fi[0], new_lr)) # append metrics, val_loss
        
        # save last + best
        model_tmp = copy.deepcopy(model)
        models.save(model_tmp)    
        torch.save(model_tmp.state_dict(), last)
        if best_fitness == fi:
            torch.save(model_tmp.state_dict(), best)
    # end epoch

    l_file.close()

    # test best.pth
    results, _, _, stats = test(
        data_dict,
        batch_size=batch_size * 2,
        imgsz=imgsz_test,
        conf_thres=0.001,
        iou_thres=0.65,
        model=load_model(yolo_struct, nc, best, G, T, device),
        dataloader=testloader,
        save_dir=save_dir,
        save_json=True,
        plots=True,
        is_coco=True
    )
    torch.cuda.empty_cache()
    # end __main__
