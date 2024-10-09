from typing import Dict

import pandas as pd
import torch

from datasets.timeseries_dataset import TimeSeriesDataset
from generator.diffcharge import DDPM
from generator.diffusion_ts import Diffusion_TS
from generator.gan.acgan import ACGAN
from generator.options import Options


class DataGenerator:
    """
    A wrapper class for generative models.
    """

    def __init__(self, model_name: str, model_params: dict = None):
        """
        Initialize the wrapper with the model name and model parameters.

        Args:
            model_name (str): The name of the generative model ('acgan', 'diffusion_ts', etc.).
            model_params (dict): A dictionary of model parameters.
        """
        self.model_name = model_name
        self.model_params = model_params if model_params is not None else {}
        self.model = None
        self.opt = Options(model_name)

        # Update opt with parameters from model_params
        for key, value in self.model_params.items():
            setattr(self.opt, key, value)

        self._initialize_model()

    def _initialize_model(self):
        """
        Initialize the model based on the model name and parameters.
        """
        model_dict = {
            "acgan": ACGAN,
            "diffusion_ts": Diffusion_TS,
            "diffcharge": DDPM,
        }
        if self.model_name in model_dict:
            model_class = model_dict[self.model_name]
            self.model = model_class(self.opt)
        else:
            raise ValueError(f"Model {self.model_name} not recognized.")


def fit(self, X):
    """
    Train the model on the given dataset.

    Args:
        X: Input data. Should be a compatible dataset object or pandas DataFrame.
    """
    if isinstance(X, pd.DataFrame):
        dataset = self._prepare_dataset(X)
    else:
        dataset = X

    sample_timeseries, sample_cond_vars = dataset[0]
    expected_seq_len = self.model.opt.seq_len
    assert (
        sample_timeseries.shape[0] == expected_seq_len
    ), f"Expected timeseries length {expected_seq_len}, but got {sample_timeseries.shape[0]}"

    if (
        hasattr(self.model_params, "conditioning_vars")
        and self.model_params.conditioning_vars
    ):
        for var in self.model_params.conditioning_vars:
            assert (
                var in sample_cond_vars.keys()
            ), f"Conditioning variable '{var}' specified in model_params.conditioning_vars not found in dataset"

    expected_input_dim = self.model.opt.input_dim
    assert sample_timeseries.shape == (
        expected_seq_len,
        expected_input_dim,
    ), f"Expected timeseries shape ({expected_seq_len}, {expected_input_dim}), but got {sample_timeseries.shape}"

    self.model.train_model(dataset)

    def generate(self, conditioning_vars):
        """
        Generate data using the trained model.

        Args:
            conditioning_vars: The conditioning variables for generation.

        Returns:
            Generated data.
        """
        return self.model.generate(conditioning_vars)

    def sample_conditioning_vars(self, dataset, num_samples, random=False):
        """
        Sample conditioning variables from the dataset.

        Args:
            dataset: The dataset to sample from.
            num_samples (int): Number of samples to generate.
            random (bool): Whether to sample randomly or from the dataset.

        Returns:
            conditioning_vars: Dictionary of conditioning variables.
        """
        return self.model.sample_conditioning_vars(dataset, num_samples, random)

    def save(self, path):
        """
        Save the model to a file.

        Args:
            path (str): The file path to save the model.
        """
        torch.save(self.model.state_dict(), path)

    def load(self, path):
        """
        Load the model from a file.

        Args:
            path (str): The file path to load the model from.
        """
        self.model.load_state_dict(torch.load(path))
        self.model.to(self.device)

    def _prepare_dataset(
        self, df: pd.DataFrame, timeseries_colname: str, conditioning_vars: Dict = None
    ):
        """
        Convert a pandas DataFrame into the required dataset format.

        Args:
            df (pd.DataFrame): The input DataFrame.

        Returns:
            dataset: The dataset in the required format.
        """
        if isinstance(df, torch.utils.data.Dataset):
            return df
        elif isinstance(df, pd.DataFrame):
            dataset = TimeSeriesDataset(
                dataframe=df,
                conditioning_vars=conditioning_vars,
                time_series_column=timeseries_colname,
            )
            return dataset
        else:
            raise TypeError("Input X must be a Dataset or a DataFrame.")
