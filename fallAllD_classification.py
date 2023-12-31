import os
import re

import numpy as np
import torch.nn.functional as F
from tqdm import tqdm

import pandas as pd
import torch
from sklearn.metrics import f1_score, accuracy_score, confusion_matrix
from torch.utils.data import DataLoader

from utils.data_load_and_EarlyStop import ResampleArrayDataset, BasicArrayDataset, EarlyStopping
from kfall_classification import cal_tp_tn_fp_fn, plot_confusion, load_dataset
from utils.feature_engineering import calculate_roll_pitch, calculate_yaw
from utils.tcn import TCN
from utils import augmentation as aug


def load_dataset_with_neck(root_dir):
    return load_dataset(root_dir, lambda arr: arr[:, :, 1:10])


def load_dataset_with_wrist(root_dir):
    return load_dataset(root_dir, lambda arr: arr[:, :, 10:19])


def load_dataset_with_waist(root_dir):
    return load_dataset(root_dir, lambda arr: arr[:, :, 19:])


def load_dataset_with_waist_wrist(root_dir):
    return load_dataset(root_dir, lambda arr: arr[:, :, 10:])


def load_dataset_with_neck_waist(root_dir):
    def preprocess_fn(arr):
        neck = arr[:, :, 1:10].copy()
        waist = arr[:, :, 19:].copy()
        combined = np.concatenate((neck, waist), axis=2)
        return combined

    return load_dataset(root_dir, preprocess_fn)


def load_dataset_with_waist_acc(root_dir):
    def preprocess_fn(arr):
        waist = arr[:, :, 19:].copy()
        acc = waist[:, :, 0:3].copy()
        return acc

    return load_dataset(root_dir, preprocess_fn)


def load_dataset_with_waist_eul_acc(root_dir):
    def preprocess_fn(arr):
        waist = arr[:, :, 19:].copy()
        acc = waist[:, :, 0:3].copy()
        gyr = waist[:, :, 3:6].copy()
        mag = waist[:, :, 6:].copy()
        euler_z, euler_x = calculate_roll_pitch(acc[:, :, 0], acc[:, :, 1], acc[:, :, 2])

        euler_y = calculate_yaw(mag[:, :, 0], mag[:, :, 1], mag[:, :, 2], euler_z, euler_x)
        # Ensure the euler angles have the same number of dimensions
        euler_x = np.expand_dims(euler_x, axis=-1)
        euler_y = np.expand_dims(euler_y, axis=-1)
        euler_z = np.expand_dims(euler_z, axis=-1)

        euler = np.concatenate((euler_x, euler_y, euler_z), axis=2)
        combined = np.concatenate((acc, euler), axis=2)
        return combined

    return load_dataset(root_dir, preprocess_fn)


def load_dataset_with_neck_waist_wrist(root_dir):
    return load_dataset(root_dir, lambda arr: arr[:, :, 1:])


class ClassificationModel2:
    def __init__(self, dataset_path, batch_size_train, batch_size_valid, batch_size_test, input_size, output_size,
                 num_channels, flatten_method, kernel_size, dropout, learning_rate, num_epochs, model_save_path,
                 augmenter, aug_name, load_method):
        self.load_method = load_method
        self.augmenter = augmenter
        self.dataset_path = dataset_path
        self.batch_size_train = batch_size_train
        self.batch_size_valid = batch_size_valid
        self.batch_size_test = batch_size_test
        self.input_size = input_size
        self.output_size = output_size
        self.num_channels = num_channels
        self.flatten_method = flatten_method
        self.kernel_size = kernel_size
        self.dropout = dropout
        self.learning_rate = learning_rate
        self.num_epochs = num_epochs
        self.model_save_path = model_save_path
        self.aug_name = aug_name

    def run(self):
        # Load data
        if self.load_method == 'wrist':
            train, valid, test = load_dataset_with_wrist(self.dataset_path)
        elif self.load_method == 'waist':
            train, valid, test = load_dataset_with_waist(self.dataset_path)
        elif self.load_method == 'waist_wrist':
            train, valid, test = load_dataset_with_waist_wrist(self.dataset_path)
        elif self.load_method == 'neck':
            train, valid, test = load_dataset_with_neck(self.dataset_path)
        elif self.load_method == 'waist_neck':
            train, valid, test = load_dataset_with_neck_waist(self.dataset_path)
        elif self.load_method == 'neck_waist_wrist':
            train, valid, test = load_dataset_with_neck_waist_wrist(self.dataset_path)
        elif self.load_method == 'waist_eul_acc':
            train, valid, test = load_dataset_with_waist_eul_acc(self.dataset_path)
        elif self.load_method == 'waist_acc':
            train, valid, test = load_dataset_with_waist_acc(self.dataset_path)


        train_set = ResampleArrayDataset(train, augmenter=self.augmenter)
        train_loader = DataLoader(train_set, batch_size=self.batch_size_train, shuffle=True)
        valid_set = BasicArrayDataset(valid)
        valid_loader = DataLoader(valid_set, batch_size=self.batch_size_valid, shuffle=False)
        test_set = BasicArrayDataset(test)
        test_loader = DataLoader(test_set, batch_size=self.batch_size_test, shuffle=False)

        window_size = self.dataset_path.split('window_sec')[1]

        if not os.path.exists(self.model_save_path):
            # If the directory doesn't exist, create it
            os.makedirs(self.model_save_path)
        early_stopping = EarlyStopping(self.model_save_path, window_size)

        # Instantiate the TCN model
        model = TCN(self.input_size, self.output_size, self.num_channels, self.kernel_size, self.dropout,
                    self.flatten_method)
        optimizer = torch.optim.Adam(model.parameters(), lr=self.learning_rate)

        # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=15, gamma=0.1)  # every 15 epoch, *0.1

        # Move the model to the GPU if available
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)

        # Create a dictionary to store the metrics during training
        # Create a dictionary to store the metrics during training
        metrics = {
            'epoch': [],
            'train_loss': [],
            'valid_loss': [],
            'valid_accuracy': [],
            'valid_f1': [],
            'valid_TPR': [],
            'valid_FPR': [],
            'valid_FP': [],
            'valid_FN': [],
            'valid_TP': [],
            'valid_TN': []
        }

        # Training loop
        for epoch in range(self.num_epochs):
            # Training loop
            model.train()  # Set the model in training mode
            train_loss = 0
            pbar = tqdm(total=len(train_loader), ncols=0)
            for x, y in train_loader:
                # Move the inputs and labels to the GPU if available
                x = x.to(device)
                y = y.to(device)

                # Clear the gradients
                optimizer.zero_grad()

                # Forward pass
                outputs = model(x)
                outputs = outputs.squeeze(1)  # shape[batch, channel]
                # Calculate the loss
                t_loss = F.binary_cross_entropy_with_logits(outputs, y.float())  # use the new function here

                train_loss += t_loss.item()
                # Backward pass
                t_loss.backward()
                # Update the parameters
                optimizer.step()
                # scheduler.step()

                pbar.update(1)

            # Track the training progress
            pbar.close()

            model.eval()
            valid_loss = 0
            valid_outputs, valid_labels = [], []

            with torch.no_grad():
                for x, y in valid_loader:
                    x = x.to(device)
                    y = y.to(device)

                    pred = model(x)
                    pred = pred.squeeze(1)
                    v_loss = F.binary_cross_entropy_with_logits(pred, y.float())
                    valid_loss += v_loss.item()

                    # Append the model predictions and true labels to their respective lists
                    binary_outputs = torch.round(torch.sigmoid(pred)).cpu().numpy()
                    valid_outputs.extend(binary_outputs)
                    valid_labels.extend(y.cpu().numpy())

            valid_loss /= len(valid_loader)

            valid_accuracy = accuracy_score(valid_labels, valid_outputs)
            valid_f1 = f1_score(valid_labels, valid_outputs, average='weighted')

            # Compute confusion matrix and extract metrics
            valid_confusion = confusion_matrix(valid_labels, valid_outputs)
            valid_FP, valid_FN, valid_TP, valid_TN = cal_tp_tn_fp_fn(valid_confusion)
            valid_TPR = valid_TP / (valid_TP + valid_FN)  # Sensitivity
            valid_FPR = valid_FP / (valid_FP + valid_TN)  # false positive rate

            print('Epoch [{}/{}], train_Loss: {:.4f}, valid_Loss: {:.4f}, accuracy: {:.4f}, f1 score: {:.4f}'.format(
                epoch + 1, self.num_epochs, train_loss / len(train_loader), valid_loss, valid_accuracy, valid_f1))

            # Save metrics for this epoch
            metrics['epoch'].append(epoch + 1)
            metrics['train_loss'].append(train_loss / len(train_loader))
            metrics['valid_loss'].append(valid_loss)
            metrics['valid_accuracy'].append(valid_accuracy)
            metrics['valid_f1'].append(valid_f1)
            metrics['valid_TPR'].append(valid_TPR)  # Added metric
            metrics['valid_FPR'].append(valid_FPR)  # Added metric
            metrics['valid_FP'].append(valid_FP)  # Added metric
            metrics['valid_FN'].append(valid_FN)  # Added metric
            metrics['valid_TP'].append(valid_TP)  # Added metric
            metrics['valid_TN'].append(valid_TN)  # Added metric

            early_stopping(epoch, valid_loss, model)
            if early_stopping.early_stop:
                print("Early stopping")
                break

        # Convert metrics dictionary to DataFrame and save as CSV
        df_metrics = pd.DataFrame(metrics)

        csv_file_name = f'./FallAllD_test_train_records/{window_size}_training_metrics_{self.aug_name}.csv'

        if not os.path.exists('./FallAllD_test_train_records'):
            # If the directory doesn't exist, create it
            os.makedirs('./FallAllD_test_train_records')

        df_metrics.to_csv(csv_file_name, index=False)

        # Test loop
        model.eval()  # Set the model in evaluation mode
        test_loss = 0
        predictions = []
        actuals = []

        with torch.no_grad():
            for x, y in test_loader:
                x = x.to(device)
                y = y.to(device)

                pred = model(x)
                pred = pred.squeeze(1)

                # Optionally compute test loss
                t_loss = F.binary_cross_entropy_with_logits(pred, y.float())
                test_loss += t_loss.item()

                # Convert predicted probabilities to binary outputs
                binary_outputs = torch.round(torch.sigmoid(pred)).cpu().numpy()

                # Append the model predictions and true labels to their respective lists
                predictions.extend(binary_outputs)
                actuals.extend(y.cpu().numpy())
                print(binary_outputs)
                print(y.cpu().numpy())

        accuracy = accuracy_score(actuals, predictions).mean()
        confusion = plot_confusion(actuals, predictions, [0, 1])

        FP, FN, TP, TN = cal_tp_tn_fp_fn(confusion)
        TPR = TP / (TP + FN)  # Sensitivity
        FPR = FP / (FP + TN)  # false positive rate
        F1 = float(f1_score(actuals, predictions, average='weighted'))

        print('Test Accuracy: {:.4f}, F1 score: {:.4f}, TPR: {:.4f}, FPR: {:.4f}'.format(accuracy, F1, TPR, FPR))
        return window_size, accuracy, F1, TPR, FPR, FP, FN, TP, TN


if __name__ == "__main__":
    # root_dir = 'C:/Repository/master/Processed_Dataset/FallAllD'
    # flatten_methods = ["last", "mean", "max"]
    #
    # for method in flatten_methods:
    #     test_metrics = {
    #         'window_size': [],
    #         'test_accuracy': [],
    #         'test_F1': [],
    #         'test_TPR': [],
    #         'test_FPR': [],
    #     }
    #
    #     for folder in os.listdir(root_dir):
    #         # Extract the last number from the folder name
    #         last_number = int(re.findall(r'\d+', folder)[-1])
    #
    #         folder_path = os.path.join(root_dir, folder)
    #
    #         # Create a dictionary to store the metrics during testing
    #         window_size, accuracy, F1, TPR, FPR, FP, FN, TP, TN = ClassificationModel2(
    #             dataset_path=folder_path,
    #             batch_size_train=16,
    #             batch_size_valid=32,
    #             batch_size_test=32,
    #             input_size=9,
    #             output_size=1,
    #             flatten_method=method,  # Changed to the current flatten method
    #             num_channels=(64,) * 5 + (128,) * 2,
    #             kernel_size=2,
    #             dropout=0.5,
    #             load_method='waist',
    #             learning_rate=0.01,
    #             num_epochs=20,
    #             model_save_path=f"./fallAllD_cla_model_waist_{method}",  # Changed to reflect the flatten method
    #             augmenter=None,
    #             aug_name=method
    #         ).run()
    #
    #         test_metrics['window_size'].append(window_size)
    #         test_metrics['test_accuracy'].append(accuracy)
    #         test_metrics['test_F1'].append(F1)
    #         test_metrics['test_TPR'].append(TPR)
    #         test_metrics['test_FPR'].append(FPR)
    #
    #     df_metrics = pd.DataFrame(test_metrics)
    #     df_metrics.to_csv(f'./fallAllD_cla_test_metrics_{method}.csv', index=False)  # Changed to reflect the flatten method
    #
    root_dir = 'C:/Repository/master/Processed_Dataset/FallAllD_Test/FallAllD_window_sec4'
    load_methods = ['waist_eul_acc']
    augmenter = aug.Augmenter([aug.Timewarp(sigma=0.2, knot=4, p=0.5)])

    test_metrics = {
        'load_method': [],
        'window_size': [],
        'test_accuracy': [],
        'test_F1': [],
        'test_TPR': [],
        'test_FPR': [],
        'FP': [],
        'FN': [],
        'TP': [],
        'TN': []
    }

    for load in load_methods:

        if load == 'waist':
            input_size = 9
        elif load == 'wrist':
            input_size = 9
        elif load == 'neck':
            input_size = 9
        elif load == 'waist_wrist':
            input_size = 18
        elif load == 'waist_neck':
            input_size = 18
        elif load == 'neck_waist_wrist':
            input_size = 27

        window_size, accuracy, F1, TPR, FPR, FP, FN, TP, TN = ClassificationModel2(
            dataset_path=root_dir,
            batch_size_train=16,
            batch_size_valid=32,
            batch_size_test=32,
            input_size=6,
            output_size=1,
            flatten_method="mean",
            num_channels=(64,) * 5 + (128,) * 2,
            kernel_size=2,
            dropout=0.5,
            load_method=load,
            learning_rate=0.01,
            num_epochs=20,
            model_save_path=f"./fallAllD_cla_model_new_euler_acc_{load}",
            augmenter=augmenter,
            aug_name=load
        ).run()
        test_metrics['load_method'].append(load)
        test_metrics['window_size'].append(window_size)
        test_metrics['test_accuracy'].append(accuracy)
        test_metrics['test_F1'].append(F1)
        test_metrics['test_TPR'].append(TPR)
        test_metrics['test_FPR'].append(FPR)
        test_metrics['FP'].append(FP)
        test_metrics['FN'].append(FN)
        test_metrics['TP'].append(FP)
        test_metrics['TN'].append(FP)
        print(test_metrics)
    df_metrics = pd.DataFrame(test_metrics)
    # df_metrics.to_csv(f'./fallAllD_cla_test_metrics_load_methods.csv', index=False)
