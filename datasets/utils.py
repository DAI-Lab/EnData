from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import mean_squared_error
from torch.utils.data import DataLoader, Dataset


def check_inverse_transform(
    normalized_dataset: Dataset, unnormalized_dataset: Dataset
) -> float:
    """
    Checks if the inverse transform of the normalized dataset matches the unnormalized dataset by computing
    the Mean Squared Error (MSE) for each row of time series data.

    Args:
        normalized_dataset (Dataset): The dataset that has been normalized.
        unnormalized_dataset (Dataset): The original dataset before normalization.

    Returns:
        float: The average MSE between the transformed and original time series across all rows.
    """
    unnormalized_df = unnormalized_dataset.data
    transformed = normalized_dataset.inverse_transform(normalized_dataset.data)

    mse_list: List[float] = []

    for idx in range(len(unnormalized_df)):
        unnormalized_timeseries = unnormalized_df.iloc[idx]["timeseries"]
        transformed_timeseries = transformed.iloc[idx]["timeseries"]

        assert (
            unnormalized_timeseries.shape == transformed_timeseries.shape
        ), "Shape mismatch between transformed and unnormalized timeseries"

        mse = mean_squared_error(unnormalized_timeseries, transformed_timeseries)
        mse_list.append(mse)

    avg_mse = np.mean(mse_list)
    print(f"Average MSE over all rows: {avg_mse}")
    return avg_mse


def prepare_dataloader(
    dataset: Dataset, batch_size: int, shuffle: bool = True
) -> DataLoader:
    """
    Prepares a DataLoader for batching the dataset.

    Args:
        dataset (Dataset): The dataset to be batched.
        batch_size (int): The size of each batch.
        shuffle (bool, optional): Whether to shuffle the dataset before batching. Defaults to True.

    Returns:
        DataLoader: The DataLoader for the dataset.
    """
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)


def split_dataset(dataset: Dataset, val_split: float = 0.1) -> Tuple[Dataset, Dataset]:
    """
    Splits a dataset into training and validation sets based on a validation split ratio.

    Args:
        dataset (Dataset): The dataset to be split.
        val_split (float, optional): The ratio of the validation set. Defaults to 0.1 (10%).

    Returns:
        Tuple[Dataset, Dataset]: The training and validation datasets.
    """
    val_size = int(len(dataset) * val_split)
    train_size = len(dataset) - val_size

    train_dataset = torch.utils.data.Subset(dataset, range(train_size))
    val_dataset = torch.utils.data.Subset(dataset, range(train_size, len(dataset)))

    return train_dataset, val_dataset


from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


def encode_conditioning_variables(
    data: pd.DataFrame, columns_to_encode: List[str], bins: int
) -> Tuple[pd.DataFrame, Dict[str, Dict[int, Any]]]:
    """
    Encodes specified columns in the DataFrame either by binning numeric columns
    or by converting categorical/string columns to integer codes. For 'weekday'
    and 'month' columns, encoding follows chronological order.

    Args:
        data (pd.DataFrame): The input DataFrame containing the data.
        columns_to_encode (List[str]): List of column names to encode.
        bins (int): Number of bins for numeric columns.

    Returns:
        Tuple[pd.DataFrame, Dict[str, Dict[int, Any]]]:
            - Encoded DataFrame.
            - Mapping dictionary for each encoded column.
    """
    encoded_data = data.copy()
    mapping: Dict[str, Dict[int, Any]] = {}

    # Define the chronological order for weekdays and months
    weekdays_order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    months_order = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]

    for col in columns_to_encode:
        if pd.api.types.is_numeric_dtype(encoded_data[col]):
            # Numeric column: Perform binning
            binned = pd.cut(encoded_data[col], bins=bins, include_lowest=True)
            encoded_data[col] = binned.cat.codes  # Assign integer codes starting from 0
            bin_intervals = binned.cat.categories
            # Create the mapping from integer code to bin interval
            bin_mapping = {
                code: str(interval) for code, interval in enumerate(bin_intervals)
            }
            mapping[col] = bin_mapping
        else:
            # Categorical/String column
            if col.lower() == "weekday":
                # Ensure consistent capitalization
                encoded_data[col] = encoded_data[col].str.capitalize()
                # Define categorical type with specified order
                encoded_data[col] = pd.Categorical(
                    encoded_data[col], categories=weekdays_order, ordered=True
                )
                encoded_data[col] = encoded_data[col].cat.codes
                # Create the mapping from code to weekday
                weekday_mapping = {code: day for code, day in enumerate(weekdays_order)}
                mapping[col] = weekday_mapping
            elif col.lower() == "month":
                # Ensure consistent capitalization
                encoded_data[col] = encoded_data[col].str.capitalize()
                # Define categorical type with specified order
                encoded_data[col] = pd.Categorical(
                    encoded_data[col], categories=months_order, ordered=True
                )
                encoded_data[col] = encoded_data[col].cat.codes
                # Create the mapping from code to month
                month_mapping = {code: month for code, month in enumerate(months_order)}
                mapping[col] = month_mapping
            else:
                # For other categorical/string columns, perform standard encoding
                encoded_data[col] = encoded_data[col].astype("category").cat.codes
                categories = data[col].astype("category").cat.categories
                category_mapping = {
                    i: category for i, category in enumerate(categories)
                }
                mapping[col] = category_mapping

    return encoded_data, mapping


def convert_generated_data_to_df(
    data: torch.Tensor,
    conditioning_vars: Dict[str, Any],
    mapping: Dict[str, Any],
) -> pd.DataFrame:
    n_timeseries, _, _ = data.shape
    data_np = data.cpu().numpy()

    decoded_conditioning_vars = {}
    for var_name, code in conditioning_vars.items():
        decoded_conditioning_vars[var_name] = mapping[var_name][code.item()]

    records = []
    for i in range(n_timeseries):
        record = decoded_conditioning_vars.copy()
        record["timeseries"] = data_np[i]
        records.append(record)
    df = pd.DataFrame.from_records(records)
    return df
