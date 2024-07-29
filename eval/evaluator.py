import numpy as np
import pandas as pd
import torch
from torch.utils.tensorboard import SummaryWriter

from eval.metrics import (
    Context_FID,
    calculate_mmd,
    calculate_period_bound_mse,
    dynamic_time_warping_dist,
    visualization,
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Evaluator:
    def __init__(self, real_dataset, model, n_dimensions, log_dir="logs"):
        self.real_dataset = real_dataset
        self.real_df = real_dataset.data
        self.n_dimensions = n_dimensions
        self.synthetic_df = self.generate_data_for_eval(model)
        self.writer = SummaryWriter(log_dir)
        self.metrics = {"dtw": [], "mse": [], "fid": []}

    def evaluate_for_user(self, user_id):
        user_log_dir = f"{self.writer.log_dir}/user_{user_id}"
        user_writer = SummaryWriter(user_log_dir)

        print("----------------------")
        print(f"Starting evaluation for user {user_id}")
        print("----------------------")

        real_user_data = self.real_df[self.real_df["dataid"] == user_id]
        syn_user_data = self.synthetic_df
        syn_user_data["dataid"] = user_id

        # Get inverse transformed data
        real_user_data_inv = self.real_dataset.inverse_transform(real_user_data)
        syn_user_data_inv = self.real_dataset.inverse_transform(syn_user_data)

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
        user_writer.add_scalar("MMD/mean", dtw_mean)
        user_writer.add_scalar("MMD/std", dtw_std)
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

        user_writer.add_figure(
            tag="TSNE",
            figure=visualization(real_data_array_inv, syn_data_array_inv, "tsne"),
        )

        user_writer.add_figure(
            tag="KDE",
            figure=visualization(real_data_array_inv, syn_data_array_inv, "kernel"),
        )

        user_writer.flush()
        user_writer.close()

    def evaluate_all_users(self):
        user_ids = self.real_df["dataid"].unique()
        for user_id in user_ids:
            self.evaluate_for_user(user_id)

        print("Final Results: \n")
        print("--------------------")
        dtw, mmd, mse, context_fid = self.get_summary_metrics()
        print(f"Mean User DTW: {dtw}")
        print(f"Mean User MMD: {mmd}")
        print(f"Mean User Bound MSE: {mse}")
        print(f"Mean User Context-FID: {context_fid}")
        print("--------------------")
        self.writer.flush()
        self.writer.close()

    def generate_data_for_eval(self, model):
        syn_ts = []

        for _, row in self.real_df.iterrows():
            month_label = torch.tensor([row["month"]]).to(device)
            day_label = torch.tensor([row["weekday"]]).to(device)
            gen_ts = model.generate([month_label, day_label]).squeeze(0).cpu().numpy()
            gen_ts = gen_ts.reshape(-1, gen_ts.shape[0])
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
        metrics_summary = {"dtw": [], "mmd": [], "mse": [], "fid": []}

        # Collect mean values for each metric
        for metric in metrics_summary.keys():
            mean_values = [value[1] for value in self.metrics[metric]]
            metrics_summary[metric] = np.mean(mean_values)

        return (
            metrics_summary["dtw"],
            metrics_summary["mmd"],
            metrics_summary["mse"],
            metrics_summary["fid"],
        )
