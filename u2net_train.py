import os
import argparse
import glob
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader
from torchvision.transforms import transforms

from data_loader import *
from model import U2NET

SAVE_FRQ = 0
CHECK_FRQ = 0

bce_loss = nn.BCELoss(reduction="mean")

train_configs = {
    "plain_resized": {
        "name": "Plain Images",
        "message": "Learning the dataset itself...",
        "transform": [Resize(512), ToTensorLab()],
        "batch_factor": 1,
    },
    "flipped_v": {
        "name": "Vertical Flips",
        "message": "Learning the vertical flips of dataset images...",
        "transform": [Resize(512), VerticalFlip(), ToTensorLab()],
        "batch_factor": 1,
    },
    "flipped_h": {
        "name": "Horizontal Flips",
        "message": "Learning the horizontal flips of dataset images...",
        "transform": [Resize(512), HorizontalFlip(), ToTensorLab()],
        "batch_factor": 1,
    },
    "rotated_l": {
        "name": "Left Rotations",
        "message": "Learning the left rotations of dataset images...",
        "transform": [Resize(512), Rotation(90), ToTensorLab()],
        "batch_factor": 1,
    },
    "rotated_r": {
        "name": "Right Rotations",
        "message": "Learning the right rotation of dataset images...",
        "transform": [Resize(512), Rotation(270), ToTensorLab()],
        "batch_factor": 1,
    },
    "crops": {
        "name": "256px Crops",
        "message": "Augmenting dataset with random crops...",
        "transform": [Resize(2304), RandomCrop(256, 0), ToTensorLab()],
        "batch_factor": 4,  # because they are smaller => we can fit more in memory
    },
    "crops_loyal": {
        "name": "Different crops",
        "message": "Augmenting dataset with different crops...",
        "transform": [Resize(2304), RandomCrop(256, 3), ToTensorLab()],
        "batch_factor": 4,  # because they are smaller => we can fit more in memory
    },
}


def dice_loss(pred, target, smooth=1.0):
    pred = pred.contiguous()
    target = target.contiguous()

    intersection = (pred * target).sum(dim=2).sum(dim=2)

    loss = 1 - (
        (2.0 * intersection + smooth)
        / (pred.sum(dim=2).sum(dim=2) + target.sum(dim=2).sum(dim=2) + smooth)
    )

    return loss.mean()


def get_args():
    parser = argparse.ArgumentParser(
        description="A program that trains ONNX model for use with rembg"
    )

    parser.add_argument(
        "-i",
        "--tra_image_dir",
        type=str,
        default="images",
        help="Directory with images.",
    )
    parser.add_argument(
        "-m",
        "--tra_masks_dir",
        type=str,
        default="masks",
        help="Directory with masks.",
    )
    parser.add_argument(
        "-s",
        "--save_frq",
        type=int,
        default=5,
        help="Frequency of saving onnx model (every X epochs).",
    )
    parser.add_argument(
        "-c",
        "--check_frq",
        type=int,
        default=5,
        help="Frequency of saving checkpoints (every X epochs).",
    )
    parser.add_argument(
        "-b",
        "--batch",
        type=int,
        default=10,
        help="Size of a single batch loaded into memory. Set to amount of processing memory in gigabytes divided by 3.",
    )
    parser.add_argument(
        "-p",
        "--plain_resized",
        type=int,
        default=5,
        help="Number of training epochs for plain_resized.",
    )
    parser.add_argument(
        "-vf",
        "--vflipped",
        type=int,
        default=2,
        help="Number of training epochs for flipped_v.",
    )
    parser.add_argument(
        "-hf",
        "--hflipped",
        type=int,
        default=2,
        help="Number of training epochs for flipped_h.",
    )
    parser.add_argument(
        "-left",
        "--rotated_l",
        type=int,
        default=2,
        help="Number of training epochs for rotated_l.",
    )
    parser.add_argument(
        "-right",
        "--rotated_r",
        type=int,
        default=2,
        help="Number of training epochs for rotated_r.",
    )
    parser.add_argument(
        "-r",
        "--rand",
        type=int,
        default=20,
        help="Number of training epochs for 256px crops.",
    )
    parser.add_argument(
        "-l",
        "--loyal",
        type=int,
        default=7,
        help="Number of training epochs for different 256px crops.",
    )

    return parser.parse_args()


def get_device():
    if torch.cuda.is_available():
        print("NVIDIA CUDA acceleration enabled")
        torch.multiprocessing.set_start_method("spawn")
        return torch.device("cuda:0")
    elif torch.backends.mps.is_available():
        print("Apple Metal Performance Shaders acceleration enabled")
        torch.multiprocessing.set_start_method("fork")
        return torch.device("mps")
    else:
        print("No GPU acceleration :/")
        return torch.device("cpu")


def save_model_as_onnx(model, device, ite_num, input_tensor_size=(1, 3, 320, 320)):
    x = torch.randn(*input_tensor_size, requires_grad=True)
    x = x.to(device)

    onnx_file_name = "{}/{}.onnx".format("saved_models", ite_num)
    torch.onnx.export(
        model,
        x,
        onnx_file_name,
        export_params=True,
        opset_version=16,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
    )
    print("Model saved to:", onnx_file_name, "\n")
    del x


def save_checkpoint(state, filename="saved_models/checkpoint.pth.tar"):
    torch.save({"state": state}, filename)


def load_checkpoint(net, optimizer, filename="saved_models/checkpoint.pth.tar"):
    if os.path.isfile(filename):
        checkpoint = torch.load(filename)
        net.load_state_dict(checkpoint["state"]["state_dict"])
        optimizer.load_state_dict(checkpoint["state"]["optimizer"])
        training_counts = checkpoint["state"]["training_counts"]
        print(f"Loading checkpoint '{filename}'...")
        return training_counts
    else:
        print(f"No checkpoint file found at '{filename}'. Starting from scratch...")
        return {
            "plain_resized": 0,
            "flipped_v": 0,
            "flipped_h": 0,
            "rotated_l": 0,
            "rotated_r": 0,
            "crops": 0,
            "crops_loyal": 0,
        }


def load_dataset(img_dir, lbl_dir, ext):
    img_list = glob.glob(os.path.join(img_dir, "*" + ext))
    lbl_list = [os.path.join(lbl_dir, os.path.basename(img)) for img in img_list]

    return img_list, lbl_list


def muti_bce_loss_fusion(d_list, labels_v):
    bce_losses = [bce_loss(d, labels_v) for d in d_list]
    dice_losses = [dice_loss(d, labels_v) for d in d_list]
    w_bce, w_dice = 1 / 3, 2 / 3
    combined_losses = [
        w_bce * bce + w_dice * dice for bce, dice in zip(bce_losses, dice_losses)
    ]
    total_loss = sum(combined_losses)
    return combined_losses[0], total_loss


def get_dataloader(tra_img_name_list, tra_lbl_name_list, transform, batch_size):
    # Dataset with given transform
    salobj_dataset = SalObjDataset(
        img_name_list=tra_img_name_list,
        lbl_name_list=tra_lbl_name_list,
        transform=transform,
    )

    cores = 8
    if batch_size == 10:
        cores = 2  # freeing up memory a bit

    # DataLoader for the dataset
    salobj_dataloader = DataLoader(
        salobj_dataset, batch_size=batch_size, shuffle=True, num_workers=cores
    )

    return salobj_dataloader


def train_model(net, optimizer, scheduler, dataloader, device):
    epoch_loss = 0.0

    for i, data in enumerate(dataloader):
        inputs = data["image"].to(device)
        labels = data["label"].to(device)
        optimizer.zero_grad()

        outputs = net(inputs)

        first_output, combined_loss = muti_bce_loss_fusion(outputs, labels)
        combined_loss.backward()
        torch.nn.utils.clip_grad_norm_(
            net.parameters(), max_norm=1.0
        )  # Clip gradients if their norm exceeds 1
        optimizer.step()
        optimizer.zero_grad()
        scheduler.step()

        epoch_loss += combined_loss.item()

        print(
            f"  Iteration: {i + 1:4}/{len(dataloader)}, "
            f"loss: {epoch_loss / (i + 1):.5f}"
        )

    return epoch_loss


def train_epochs(
    net, optimizer, scheduler, dataloader, device, epochs, training_counts, key
):
    for epoch in epochs:
        start_time = time.time()

        # this is where the training occurs!
        print(f"Epoch: {epoch + 1}/{epochs[-1] + 1}")
        epoch_loss = train_model(net, optimizer, scheduler, dataloader, device)
        print(f"Loss per epoch: {epoch_loss}\n")

        if sum(training_counts.values()) == 3:
            elapsed_time = time.time() - start_time
            minutes, seconds = divmod(elapsed_time, 60)
            perf = minutes + (seconds / 60)
            print(f"Expected performance is {perf:.1f} minutes per epoch.\n")
        # Increment the corresponding training count
        training_counts[key] += 1

        # Saves model every save_frq iterations or during the last one
        if (epoch + 1) % SAVE_FRQ == 0 or epoch + 1 == len(epochs):
            # in ONNX format! ^_^ UwU
            save_model_as_onnx(net, device, sum(training_counts.values()))

        # Saves checkpoint every check_frq epochs or during the last one
        if (epoch + 1) % CHECK_FRQ == 0 or epoch + 1 == len(epochs):
            save_checkpoint(
                {
                    "epoch_count": epoch + 1,
                    "state_dict": net.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "training_counts": training_counts,
                }
            )
            if epoch + 1 < len(epochs):
                print("Checkpoint made\n")

    return net


def main():
    device = get_device()

    args = get_args()
    global SAVE_FRQ, CHECK_FRQ
    SAVE_FRQ = args.save_frq
    CHECK_FRQ = args.check_frq
    tra_image_dir = args.tra_image_dir
    tra_label_dir = args.tra_masks_dir
    batch = args.batch

    targets = {
        "plain_resized": args.plain_resized,
        "flipped_h": args.hflipped,
        "flipped_v": args.vflipped,
        "rotated_l": args.rotated_l,
        "rotated_r": args.rotated_r,
        "crops": args.rand,
        "crops_loyal": args.loyal,
    }

    if not os.path.exists("saved_models"):
        os.makedirs("saved_models")

    tra_img_name_list, tra_lbl_name_list = load_dataset(
        tra_image_dir, tra_label_dir, ".png"
    )

    print(
        "Images: {},".format(len(tra_img_name_list)), "masks:", len(tra_lbl_name_list)
    )

    if len(tra_img_name_list) != len(tra_lbl_name_list):
        print("Different amounts of images and masks, can't proceed mate")
        return

    net = U2NET(3, 1)
    net.to(device)
    net.train()

    optimizer = optim.Adam(
        net.parameters(), lr=0.001, betas=(0.9, 0.999), eps=1e-08, weight_decay=0
    )

    training_counts = load_checkpoint(net, optimizer)
    # dealing with negative values, if model was trained for more epochs than in target:
    for key in training_counts:
        if targets[key] < training_counts[key]:
            targets[key] = training_counts[key]
        print(
            f"Task: {train_configs[key]['name']:<17} Epochs done: {training_counts[key]}/{targets[key]}"
        )

    print("---\n")

    scheduler = CosineAnnealingLR(optimizer, T_max=sum(targets.values()), eta_min=1e-6)

    def create_and_train(transform, batch_size, epochs, train_type):
        """Creates a dataloader and trains the network using the given parameters."""
        dataloader = get_dataloader(
            tra_img_name_list, tra_lbl_name_list, transform, batch_size
        )
        train_epochs(
            net,
            optimizer,
            scheduler,
            dataloader,
            device,
            epochs,
            training_counts,
            train_type,
        )

    # Training loop
    for train_type, config in train_configs.items():
        if training_counts[train_type] < targets[train_type]:
            print(config["message"])
            epochs = range(training_counts[train_type], targets[train_type])
            transform = transforms.Compose(config["transform"])

            create_and_train(
                transform, batch * config["batch_factor"], epochs, train_type
            )

            training_counts[train_type] = targets[train_type]

    print("Nothing left to do!")


if __name__ == "__main__":
    main()
