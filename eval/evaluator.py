import random

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from torch.utils.tensorboard import SummaryWriter

from data_utils.dataset import PecanStreetDataset, split_dataset
from data_utils.utils import check_inverse_transform
from eval.discriminative_metric import discriminative_score_metrics
from eval.metrics import (
    Context_FID,
    calculate_mmd,
    calculate_period_bound_mse,
    dynamic_time_warping_dist,
    plot_range_with_syn_values,
    visualization,
)
from eval.predictive_metric import predictive_score_metrics
from generator.acgan import ACGAN
from generator.diffcharge.diffusion import DDPM
from generator.diffcharge.options import Options
from generator.diffusion_ts.gaussian_diffusion import Diffusion_TS
from generator.timegan import TimeGAN

USER_IDS = [
    3687,
    6377,
    7062,
    8574,
    9213,
    203,
    1450,
    1524,
    2606,
    3864,
    7114,
    1731,
    4495,
    8342,
    3938,
    5938,
    8061,
    9775,
    4934,
    8733,
    9612,
    9836,
    6547,
    661,
    1642,
    2335,
    2361,
    2818,
    3039,
    3456,
    3538,
    4031,
    4373,
    4767,
    5746,
    6139,
    7536,
    7719,
    7800,
    7901,
    7951,
    8156,
    8386,
    8565,
    9019,
    9160,
    9922,
    9278,
    4550,
    558,
    2358,
    3700,
    1417,
    5679,
    5058,
    2318,
    5997,
    950,
    5982,
    5587,
    1222,
    387,
    3000,
    4283,
    3488,
    3517,
    9053,
    3996,
    27,
    142,
    914,
    2096,
    1240,
]

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Evaluator:
    def __init__(self, real_dataset, model_name, log_dir="logs"):
        self.real_dataset = real_dataset
        self.real_dataset = real_dataset
        self.model_name = model_name
        self.writer = SummaryWriter(log_dir)
        self.metrics = {
            "dtw": [],
            "mmd": [],
            "mse": [],
            "fid": [],
            "discr": [],
            "pred": [],
        }

    def evaluate_for_user(self, user_id):

        user_dataset = self.real_dataset.create_user_dataset(user_id)
        model = self.get_trained_model_for_user(self.model_name, user_dataset)

        user_log_dir = f"{self.writer.log_dir}/user_{user_id}"
        user_writer = SummaryWriter(user_log_dir)

        print("----------------------")
        print(f"Starting evaluation for user {user_id}")
        print("----------------------")

        real_user_data = user_dataset.data
        syn_user_data = self.generate_data_for_eval(model)
        syn_user_data["dataid"] = user_id

        # Get inverse transformed data
        real_user_data_inv = self.user_dataset.inverse_transform(real_user_data)
        syn_user_data_inv = self.user_dataset.inverse_transform(syn_user_data)

        # TODO add assertion of inverste transformation success

        real_data_array = np.stack(real_user_data["timeseries"])
        syn_data_array = np.stack(syn_user_data["timeseries"])
        real_data_array_inv = np.stack(real_user_data_inv["timeseries"])
        syn_data_array_inv = np.stack(syn_user_data_inv["timeseries"])

        # Compute dtw using original scale data
        dtw_mean, dtw_std = dynamic_time_warping_dist(
            real_data_array_inv, syn_data_array_inv
        )
        user_writer.add_scalar("DTW/mean", dtw_mean)
        user_writer.add_scalar("DTW/std", dtw_std)
        self.metrics["dtw"].append((user_id, dtw_mean, dtw_std))

        # Compute maximum mean discrepancy between real and synthetic data for all daily load profiles and get mean
        mmd_mean, mmd_std = calculate_mmd(real_data_array_inv, syn_data_array_inv)
        user_writer.add_scalar("MMD/mean", mmd_mean)
        user_writer.add_scalar("MMD/std", mmd_std)
        self.metrics["mmd"].append((user_id, mmd_mean, mmd_std))

        # Compute Period Bound MSE using original scale data and dataframe
        mse_mean, mse_std = calculate_period_bound_mse(
            syn_data_array_inv, real_user_data_inv
        )
        user_writer.add_scalar("MSE/mean", mse_mean)
        user_writer.add_scalar("MSE/std", mse_std)
        self.metrics["mse"].append((user_id, mse_mean, mse_std))

        # Compute Context FID using normalized and scaled data
        print(f"Training TS2Vec for user {user_id}...")
        fid_score = Context_FID(real_data_array, syn_data_array)
        print("Done!")
        user_writer.add_scalar("FID/score", fid_score)
        self.metrics["fid"].append((user_id, fid_score))

        # Compute discriminative score using original scale data
        print(f"Computing discriminative score for user {user_id}...")
        discr_score, _, _ = discriminative_score_metrics(
            real_data_array_inv, syn_data_array_inv
        )
        print("Done!")
        user_writer.add_scalar("Discr/score", discr_score)
        self.metrics["discr"].append((user_id, discr_score))

        # Compute predictive score using original scale data
        print(f"Computing predictive score for user {user_id}...")
        pred_score = predictive_score_metrics(real_data_array_inv, syn_data_array_inv)
        print("Done!")
        user_writer.add_scalar("Pred/score", pred_score)
        self.metrics["pred"].append((user_id, pred_score))

        # Randomly select three months and three weekdays
        unique_months = self.real_df["month"].unique()
        unique_weekdays = self.real_df["weekday"].unique()
        selected_months = random.sample(list(unique_months), 1)
        selected_weekdays = random.sample(list(unique_weekdays), 1)

        # Add KDE plot
        plots = visualization(real_data_array_inv, syn_data_array_inv, "kernel")
        for i, plot in enumerate(plots):
            user_writer.add_figure(tag=f"KDE Dimension {i}", figure=plot)

        # Plot the range and synthetic values for each combination
        for month in selected_months:
            for weekday in selected_weekdays:
                fig = plot_range_with_syn_values(
                    self.real_df, self.synthetic_df, month, weekday
                )
                user_writer.add_figure(
                    tag=f"Range_Plot_Month_{month}_Weekday_{weekday}",
                    figure=fig,
                )

        user_writer.flush()
        user_writer.close()

    def evaluate_all_users(self):
        user_ids = self.real_dataset.data["dataid"].unique()
        for user_id in user_ids:
            self.evaluate_for_user(user_id)

        print("Final Results: \n")
        print("--------------------")
        dtw, mmd, mse, context_fid, discr_score, pred_score = self.get_summary_metrics()
        print(f"Mean User DTW: {dtw}")
        print(f"Mean User MMD: {mmd}")
        print(f"Mean User Bound MSE: {mse}")
        print(f"Mean User Context-FID: {context_fid}")
        print(f"Mean User Discriminative Score: {discr_score}")
        print(f"Mean User Predictive Score: {pred_score}")
        print("--------------------")
        self.writer.flush()
        self.writer.close()

    def generate_data_for_eval(self, model):
        month_labels = torch.tensor(self.real_df["month"].values).to(device)
        day_labels = torch.tensor(self.real_df["weekday"].values).to(device)

        generated_ts = (
            model.generate(month_labels=month_labels, day_labels=day_labels)
            .cpu()
            .numpy()
        )

        if len(generated_ts.shape) == 2:
            generated_ts = generated_ts.reshape(
                generated_ts.shape[0], -1, generated_ts.shape[1]
            )

        syn_ts = []
        for idx, (_, row) in enumerate(self.real_df.iterrows()):
            gen_ts = generated_ts[idx]
            syn_ts.append((row["month"], row["weekday"], row["date_day"], gen_ts))

        columns = ["month", "weekday", "date_day", "timeseries"]
        syn_ts_df = pd.DataFrame(syn_ts, columns=columns)
        return syn_ts_df

    def get_summary_metrics(self):
        """
        Calculate the mean values for all users across all metrics.

        Returns:
            A tuple containing the mean values for dtw, mse, and fid.
        """
        metrics_summary = {
            "dtw": [],
            "mmd": [],
            "mse": [],
            "fid": [],
            "discr": [],
            "pred": [],
        }

        # Collect mean values for each metric
        for metric in metrics_summary.keys():
            mean_values = [value[1] for value in self.metrics[metric]]
            metrics_summary[metric] = np.mean(mean_values)

        return (
            metrics_summary["dtw"],
            metrics_summary["mmd"],
            metrics_summary["mse"],
            metrics_summary["fid"],
            metrics_summary["discr"],
            metrics_summary["pred"],
        )

    def get_trained_model_for_user(self, model_name, user_dataset):

        input_dim = (
            int(user_dataset.is_pv_user) + 1
        )  # if user has available pv data, input dim is 2

        if model_name == "acgan":
            train_dataset, val_dataset = split_dataset(user_dataset)

            model = ACGAN(
                input_dim=input_dim,
                noise_dim=2,
                embedding_dim=256,
                window_length=96,
                learning_rate=1e-4,
                weight_path="runs/",
            )
            model.train(train_dataset, val_dataset, batch_size=32, num_epoch=200)

        elif model_name == "diffcharge":
            train_dataset, val_dataset = split_dataset(user_dataset)
            opt = Options("diffusion", isTrain=True)
            model = DDPM(opt=opt)
            model.train(train_dataset, val_dataset, batch_size=32, num_epoch=200)

        elif model_name == "diffusion_ts":
            model = Diffusion_TS(seq_length=96, feature_size=input_dim, d_model=96)
            model.train_model(user_dataset, batch_size=32)

        else:
            raise ValueError("Model name not recognized!")
