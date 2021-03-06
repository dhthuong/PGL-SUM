# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import os
import random
import json
import h5py
from tqdm.notebook import tqdm, trange
from layers.summarizer import PGL_SUM
from utils import TensorboardWriter
from generate_summary import generate_summary

def evaluate_summary(predicted_summary, user_summary, eval_method):
    """ Compare the predicted summary with the user defined one(s).

    :param ndarray predicted_summary: The generated summary from our model.
    :param ndarray user_summary: The user defined ground truth summaries (or summary).
    :param str eval_method: The proposed evaluation method; either 'max' (SumMe) or 'avg' (TVSum).
    :return: The reduced fscore based on the eval_method
    """
    max_len = max(len(predicted_summary), user_summary.shape[1])
    S = np.zeros(max_len, dtype=int)
    G = np.zeros(max_len, dtype=int)
    S[:len(predicted_summary)] = predicted_summary

    f_scores = []
    for user in range(user_summary.shape[0]):
        G[:user_summary.shape[1]] = user_summary[user]
        overlapped = S & G

        # Compute precision, recall, f-score
        precision = sum(overlapped)/sum(S)
        recall = sum(overlapped)/sum(G)
        if precision+recall == 0:
            f_scores.append(0)
        else:
            f_scores.append(2 * precision * recall * 100 / (precision + recall))

    if eval_method == 'max':
        return max(f_scores)
    else:
        return sum(f_scores)/len(f_scores)

class Solver(object):
    def __init__(self, config=None, train_loader=None, test_loader=None, train_infer_loader=None):
        """Class that Builds, Trains and Evaluates PGL-SUM model"""
        # Initialize variables to None, to be safe
        self.model, self.optimizer, self.writer = None, None, None

        self.config = config
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.train_infer_loader = train_infer_loader 

        # Set the seed for generating reproducible random numbers
        if self.config.seed is not None:
            torch.manual_seed(self.config.seed)
            torch.cuda.manual_seed_all(self.config.seed)
            np.random.seed(self.config.seed)
            random.seed(self.config.seed)

    def build(self):
        """ Function for constructing the PGL-SUM model of its key modules and parameters."""
        # Model creation
        self.model = PGL_SUM(input_size=self.config.input_size,
                             output_size=self.config.input_size,
                             num_segments=self.config.n_segments,
                             heads=self.config.heads,
                             fusion=self.config.fusion,
                             pos_enc=self.config.pos_enc).to(self.config.device)
        if self.config.init_type is not None:
            self.init_weights(self.model, init_type=self.config.init_type, init_gain=self.config.init_gain)

        if self.config.mode == 'train':
            # Optimizer initialization
            self.optimizer = optim.Adam(self.model.parameters(), lr=self.config.lr, weight_decay=self.config.l2_req)
            self.writer = TensorboardWriter(str(self.config.log_dir))

    @staticmethod
    def init_weights(net, init_type="xavier", init_gain=1.4142):
        """ Initialize 'net' network weights, based on the chosen 'init_type' and 'init_gain'.

        :param nn.Module net: Network to be initialized.
        :param str init_type: Name of initialization method: normal | xavier | kaiming | orthogonal.
        :param float init_gain: Scaling factor for normal.
        """
        for name, param in net.named_parameters():
            if 'weight' in name and "norm" not in name:
                if init_type == "normal":
                    nn.init.normal_(param, mean=0.0, std=init_gain)
                elif init_type == "xavier":
                    nn.init.xavier_uniform_(param, gain=np.sqrt(2.0))  # ReLU activation function
                elif init_type == "kaiming":
                    nn.init.kaiming_uniform_(param, mode="fan_in", nonlinearity="relu")
                elif init_type == "orthogonal":
                    nn.init.orthogonal_(param, gain=np.sqrt(2.0))      # ReLU activation function
                else:
                    raise NotImplementedError(f"initialization method {init_type} is not implemented.")
            elif 'bias' in name:
                nn.init.constant_(param, 0.1)

    criterion = nn.MSELoss()

    def train(self):
        last_loss = 100000
        tol = 7
        logfile = open('trainlog.txt', 'w')

        """ Main function to train the PGL-SUM model. """
        for epoch_i in trange(self.config.n_epochs, desc='Epoch', ncols=80):
            self.model.train()
            f1_score = []
            loss_history = []

            num_batches = int(len(self.train_loader) / self.config.batch_size)  # full-batch or mini batch
            iterator = iter(self.train_loader)
            for _ in trange(num_batches, desc='Batch', ncols=80, leave=False):
                # ---- Training ... ----#
                if self.config.verbose:
                    tqdm.write('Time to train the model...')

                self.optimizer.zero_grad()
                for _ in trange(self.config.batch_size, desc='Video', ncols=80, leave=False):
                    frame_features, target, _= next(iterator)
                    user_summary = target.numpy()

                    frame_features = frame_features.to(self.config.device)
                    target = target.to(self.config.device)

                    output, weights = self.model(frame_features.squeeze(0))
                    model_summary = output.cpu().detach().numpy().reshape((-1))
                    loss = self.criterion(output.squeeze(0), target.squeeze(0))

                    f1 = evaluate_summary(model_summary, user_summary, 'max')
                    if not np.isnan(f1):
                        f1_score.append(f1)

                    if self.config.verbose:
                        tqdm.write(f'[{epoch_i}] loss: {loss.item()}')

                    loss.backward()
                    loss_history.append(loss.data)
                # Update model parameters every 'batch_size' iterations
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.clip)
                self.optimizer.step()

            # Mean loss of each training step
            loss = torch.stack(loss_history).mean()

            # Early stopping
            current_loss = loss.cpu().detach().numpy()
            diff = (current_loss-last_loss)/current_loss* 100
            last_loss = current_loss
            if diff >= tol:
                break

            # Plot
            if self.config.verbose:
                tqdm.write('Plotting...')

            self.writer.update_loss(loss, epoch_i, 'loss_epoch')
            # Uncomment to save parameters at checkpoint
            if not os.path.exists(self.config.save_dir):
                os.makedirs(self.config.save_dir)
            #  ckpt_path = str(self.config.save_dir) + f'/epoch-{epoch_i}.pkl'
            ckpt_path = str(self.config.save_dir) + '/last_epoch.pkl'
            tqdm.write(f'Save parameters at {ckpt_path}')

            torch.save(self.model.state_dict(), ckpt_path)


            f1_train, f1_test = self.evaluate(epoch_i)
            self.writer.update_loss(f1_train, epoch_i, 'f1_train')
            self.writer.update_loss(f1_test, epoch_i, 'f1_test')

            logfile.write('epoch:' + str(epoch_i) + ' loss:' + str(current_loss) + ' diff:' + str(diff) \
                    + ' f1_train:' + str(f1_train) + ' f1_test:' + str(f1_test) + '\n') 
            print('epoch:' + str(epoch_i) + ' loss:' + str(current_loss) + ' diff:' + str(diff) \
                    + ' f1_train:' + str(f1_train) + ' f1_test:' + str(f1_test) ) 

    def set_summary_from_video_index(self, hdf, video_index, scores):
        sb = np.array(hdf.get('video_' + video_index + '/change_points'))
        n_frames = np.array(hdf.get('video_' + video_index + '/n_frames'))
        positions = np.array(hdf.get('video_' + video_index + '/picks'))
        summary = generate_summary(sb, scores, n_frames, positions)
        return summary

    def evaluate(self, epoch_i, save_weights=False):
        """ Saves the frame's importance scores for the test videos in json format.

        :param int epoch_i: The current training epoch.
        :param bool save_weights: Optionally, the user can choose to save the attention weights in a (large) h5 file.
        """
        self.model.eval()

        weights_save_path = self.config.score_dir.joinpath("weights.h5")
        out_scores_test = {}
        f1_test = []
        f1_train = []

        dataset_path = '../PGL-SUM/data/datasets/' + 'SumMe' + '/eccv16_dataset_' + 'summe' + '_google_pool5.h5'

        hdf = h5py.File(dataset_path, 'r')


        # For test
        for frame_features, gt_scores, video_name in tqdm(self.test_loader, desc='Evaluate_test', ncols=80, leave=False):
            # [seq_len, input_size]
            frame_features = frame_features.view(-1, self.config.input_size).to(self.config.device)

            with torch.no_grad():
                scores, attn_weights = self.model(frame_features)  # [1, seq_len]
                scores = scores.squeeze(0).cpu().numpy().tolist()
                attn_weights = attn_weights.cpu().numpy()

                out_scores_test[video_name] = scores

            # Compute F1 score for test
            video_index = video_name[6:]
            user_summary = np.array(hdf.get('video_' + video_index + '/user_summary'))
            summary = self.set_summary_from_video_index(hdf, video_index, scores)
            f1_score = evaluate_summary(summary, user_summary, 'max')
            f1_test.append(f1_score)

        avg_f1_test = np.mean(f1_test)
        
        # For train
        for frame_features, gt_scores, video_name in tqdm(self.train_infer_loader, desc='Evaluate_train', ncols=80, leave=False):
            # [seq_len, input_size]
            frame_features = frame_features.view(-1, self.config.input_size).to(self.config.device)

            with torch.no_grad():
                scores, attn_weights = self.model(frame_features)  # [1, seq_len]
                scores = scores.squeeze(0).cpu().numpy().tolist()
                attn_weights = attn_weights.cpu().numpy()

            # Compute F1 score for test
            video_index = video_name[6:]
            user_summary = np.array(hdf.get('video_' + video_index + '/user_summary'))
            summary = self.set_summary_from_video_index(hdf, video_index, scores)

            f1_score = evaluate_summary(summary, user_summary, 'max')
            f1_train.append(f1_score)
    
        avg_f1_train = np.mean(f1_train)

        return avg_f1_train, avg_f1_test
if __name__ == '__main__':
    pass
