import numpy as np
import pandas as pd
from sklearn.metrics import mean_squared_error
from tqdm import tqdm

from data_utils.dataset import PecanStreetDataset, split_dataset
from eval.evaluator import Evaluator
from generator.acgan import ACGAN
from generator.diffusion_ts.gaussian_diffusion import Diffusion_TS


def evaluate_acgan():
    full_dataset = PecanStreetDataset(
        normalize=True, user_id=None, include_generation=True, threshold=(-6, 6)
    )
    all_users = full_dataset.data.dataid.unique()
    all_users = [661]

    for user in tqdm(all_users):
        print(f"Training for user {user}...")
        data = PecanStreetDataset(
            normalize=True, user_id=user, include_generation=True, threshold=(-6, 6)
        )
        train_dataset, val_dataset = split_dataset(data)

        input_dim = (
            int(data.is_pv_user) + 1
        )  # if user has available pv data, input dim is 2

        model = ACGAN(
            input_dim=input_dim,
            noise_dim=512,
            embedding_dim=512,
            window_length=96,
            learning_rate=1e-4,
            weight_path="runs/",
        )
        model.train(train_dataset, val_dataset, batch_size=32, num_epoch=200)
        user_evaluator = Evaluator(data, model, input_dim, f"runs/user_{user}")
        user_evaluator.evaluate_all_users()


def main():

    model = Diffusion_TS(seq_length=96, feature_size=2, d_model=96)

    data = PecanStreetDataset(
        normalize=True, user_id=661, include_generation=True, threshold=(-6, 6)
    )

    input_dim = int(data.is_pv_user) + 1

    model.train_model(data, batch_size=32)
    user_evaluator = Evaluator(data, model, input_dim, f"runs/user_{661}")
    user_evaluator.evaluate_all_users()
    # evaluate_acgan()


if __name__ == "__main__":
    main()
