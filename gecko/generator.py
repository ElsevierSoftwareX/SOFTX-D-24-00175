"""
The generator module provides generator functions for generating realistic data.
These generators wrap around common data sources such as frequency tables and numeric distributions.
"""

__all__ = [
    "Generator",
    "from_function",
    "from_uniform_distribution",
    "from_normal_distribution",
    "from_frequency_table",
    "from_multicolumn_frequency_table",
    "to_data_frame",
]

from os import PathLike
from typing import Callable, Optional, Union, Any

import numpy as np
import pandas as pd
from typing_extensions import ParamSpec  # required for 3.9 backport

P = ParamSpec("P")
Generator = Callable[[int], list[pd.Series]]


def from_function(
    func: Callable[P, str], *args: tuple[Any, ...], **kwargs: dict[str, Any]
) -> Generator:
    """
    Generate data from an arbitrary function that returns a single value at a time.

    Notes:
        This function should be used sparingly since it is not vectorized.
        Only use it for testing purposes or if performance is not important.

    Args:
        func: function to invoke to generate data from
        *args: positional arguments to pass to `func`
        **kwargs: keyword arguments to pass to `func`

    Returns:
        function returning list with strings generated from custom function
    """

    def _generate(count: int) -> list[pd.Series]:
        return [pd.Series(data=[func(*args, **kwargs) for _ in np.arange(count)])]

    return _generate


def from_uniform_distribution(
    low: Union[int, float] = 0,
    high: Union[int, float] = 1,
    precision: int = 6,
    rng: Optional[np.random.Generator] = None,
) -> Generator:
    """
    Generate data from a uniform distribution.

    Args:
        low: lower limit of uniform distribution (inclusive)
        high: upper limit of uniform distribution (exclusive)
        precision: decimal precision of the numbers generated from the uniform distribution
        rng: random number generator to use

    Returns:
        function returning list with numbers drawn from a uniform distribution formatted as strings
    """
    if rng is None:
        rng = np.random.default_rng()

    format_str = f"%.{precision}f"

    def _generate(count: int) -> list[pd.Series]:
        return [pd.Series(np.char.mod(format_str, rng.uniform(low, high, count)))]

    return _generate


def from_normal_distribution(
    mean: float = 0,
    sd: float = 1,
    precision: int = 6,
    rng: Optional[np.random.Generator] = None,
) -> Generator:
    """
    Generate data from a normal distribution.

    Args:
        mean: mean of the normal distribution
        sd: standard deviation of the normal distribution
        precision: decimal precision of the numbers generated from the normal distribution
        rng: random number generator to use

    Returns:
        function returning list with numbers drawn from a normal distribution formatted as strings
    """
    if rng is None:
        rng = np.random.default_rng()

    format_str = f"%.{precision}f"

    def _generate(count: int) -> list[pd.Series]:
        return [pd.Series(np.char.mod(format_str, rng.normal(mean, sd, count)))]

    return _generate


def from_frequency_table(
    csv_file_path: Union[str, PathLike[str]],
    header: bool = False,
    value_column: Union[str, int] = 0,
    freq_column: Union[str, int] = 1,
    encoding: str = "utf-8",
    delimiter: str = ",",
    rng: Optional[np.random.Generator] = None,
) -> Generator:
    """
    Generate data from a frequency table.
    The frequency table must be provided in CSV format and contain at least two columns: one containing values to
    generate and one containing their assigned absolute frequencies.
    Values generated by this function will have a distribution similar to the frequencies listed in the input file.

    Args:
        csv_file_path: path to CSV file
        header: `True` if the CSV file contains a header row, `False` otherwise
        value_column: name or index of the value column
        freq_column: name or index of the frequency column
        encoding: character encoding of the CSV file
        delimiter: column delimiter of the CSV file
        rng: random number generator to use

    Returns:
        function returning list with single series containing values generated from the input file
    """
    if rng is None:
        rng = np.random.default_rng()

    if type(value_column) is not type(freq_column):
        raise ValueError("value and frequency column must both be of the same type")

    # read csv file
    df = pd.read_csv(
        csv_file_path,
        header=0 if header else None,  # header row index (`None` if not present)
        usecols=[value_column, freq_column],
        dtype={freq_column: "int", value_column: "str"},
        sep=delimiter,
        encoding=encoding,
    )

    # convert absolute to relative frequencies
    srs_value = df[value_column]
    srs_prob = df[freq_column] / df[freq_column].sum()

    def _generate(count: int) -> list[pd.Series]:
        return [pd.Series(rng.choice(srs_value, count, p=srs_prob))]

    return _generate


def from_multicolumn_frequency_table(
    csv_file_path: Union[str, PathLike[str]],
    header: bool = False,
    value_columns: Union[int, str, list[int], list[str]] = 0,
    freq_column: Union[int, str] = 1,
    encoding: str = "utf-8",
    delimiter: str = ",",
    rng: Optional[np.random.Generator] = None,
) -> Generator:
    """
    Generate data from a frequency table with multiple interdependent columns..
    The frequency table must be provided in CSV format and contain at least two columns: one containing values to
    generate and one containing their assigned absolute frequencies.
    Values generated by this function will have a distribution similar to the frequencies listed in the input file.

    Args:
        csv_file_path: path to CSV file
        header: `True` if the CSV file contains a header row, `False` otherwise
        value_columns: names or indices of the value columns
        freq_column: name or index of the frequency column
        encoding: character encoding of the CSV file
        delimiter: column delimiter of the CSV file
        rng: random number generator to use

    Returns:
        function returning list with as many series as there are value columns specified containing values generated from the input file
    """
    if rng is None:
        rng = np.random.default_rng()

    # if the value columns are a list, then read the type of its entries from the first one
    if isinstance(value_columns, list):
        if len(value_columns) == 0:
            raise ValueError("value column list cannot be empty")

        value_columns_type = type(value_columns[0])
    else:
        value_columns_type = type(value_columns)

    if value_columns_type is not type(freq_column):
        raise ValueError("value and frequency column must both be of the same type")

    # if value_columns is an int or str, wrap it into a list
    value_columns = (
        [value_columns] if not isinstance(value_columns, list) else value_columns
    )

    df = pd.read_csv(
        csv_file_path,
        header=0 if header else None,
        usecols=value_columns + [freq_column],
        dtype={
            freq_column: "int",
            **{value_column: "str" for value_column in value_columns},
        },
        sep=delimiter,
        encoding=encoding,
    )

    # sum of absolute frequencies
    freq_total = df[freq_column].sum()
    # new series to track the relative frequencies
    value_tuple_list = list(zip(*[list(df[c]) for c in value_columns]))
    rel_freq_list = list(df[freq_column] / freq_total)

    # noinspection PyTypeChecker
    def _generate(count: int) -> list[pd.Series]:
        x = rng.choice(value_tuple_list, count, p=rel_freq_list)
        return [pd.Series(list(t)) for t in zip(*x)]  # dark magic

    return _generate


def to_data_frame(
    column_to_generator_dict: dict[Union[str, tuple[str, ...]], Generator],
    count: int,
) -> pd.DataFrame:
    """
    Generate data frame by using multiple generators at once.
    Column names must be mapped to their respective generators.
    A generator can be assigned to one or multiple column names, but it must always match the amount of series
    that the generator returns.

    Args:
        column_to_generator_dict: mapping of column names to generators
        count: amount of records to generate

    Returns:
        data frame with columns and rows generated as specified
    """
    if len(column_to_generator_dict) == 0:
        raise ValueError("generator dict may not be empty")

    if count <= 0:
        raise ValueError(f"amount of rows must be positive, is {count}")

    col_to_srs_dict: dict[str, pd.Series] = {}

    for gen_col_names, gen in column_to_generator_dict.items():
        # if a single string is provided, concat by wrapping it into a list
        if isinstance(gen_col_names, str):
            gen_col_names = (gen_col_names,)

        # generate values
        gen_col_values = gen(count)

        # check that the generator returned as many columns as expected
        if len(gen_col_values) != len(gen_col_names):
            raise ValueError(
                f"generator returned {len(gen_col_values)} columns, but requires {len(gen_col_names)} to "
                f"fill column(s) for: {','.join(gen_col_names)}"
            )

        # assign name to series
        for i in range(len(gen_col_values)):
            col_to_srs_dict[gen_col_names[i]] = gen_col_values[i]

    # finally create df from the list of named series
    return pd.DataFrame(data=col_to_srs_dict)
