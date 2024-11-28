import hydra
import numpy as np
import pandas as pd
from hydra import compose, initialize_config_dir
from omegaconf import DictConfig, OmegaConf

from datasets.openpower import OpenPowerDataset
from datasets.pecanstreet import PecanStreetDataset
from datasets.timeseries_dataset import TimeSeriesDataset
from endata.data_generator import DataGenerator
from eval.evaluator import Evaluator


def evaluate_single_dataset_model(cfg: DictConfig):
    if cfg.dataset.name == "pecanstreet":
        dataset = PecanStreetDataset(cfg.dataset)
    elif cfg.dataset.name == "openpower":
        dataset = OpenPowerDataset(cfg.dataset)

    non_pv_user_evaluator = Evaluator(cfg, dataset)
    non_pv_user_evaluator.evaluate_model()


@hydra.main(version_base=None, config_path="config", config_name="config")
def main(cfg: DictConfig):
    evaluate_single_dataset_model(cfg=cfg)
    with open("config_used.yaml", "w") as f:
        OmegaConf.save(cfg, f)


if __name__ == "__main__":
    main()
