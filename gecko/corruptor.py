__all__ = [
    "Corruptor",
    "with_cldr_keymap_file",
    "with_phonetic_replacement_table",
    "with_replacement_table",
    "with_missing_value",
    "with_insert",
    "with_delete",
    "with_transpose",
    "with_substitute",
    "with_edit",
    "with_noop",
    "with_categorical_values",
    "corrupt_dataframe",
]

import string
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Callable, Optional, Union, Literal, NamedTuple, NoReturn

import numpy as np
import pandas as pd
from lxml import etree

from gecko.cldr import decode_iso_kb_pos, unescape_kb_char, get_neighbor_kb_pos_for

Corruptor = Callable[[pd.Series], pd.Series]
_EditOp = Literal["ins", "del", "sub", "trs"]


class _PhoneticReplacementRule(NamedTuple):
    pattern: str
    replacement: str
    flags: str


def _check_probability_in_bounds(p: float):
    if p < 0 or p > 1:
        raise ValueError("probability is out of range, must be between 0 and 1")


@dataclass(frozen=True)
class KeyMutation:
    row: list[str] = field(default_factory=list)
    col: list[str] = field(default_factory=list)


def with_cldr_keymap_file(
    cldr_path: Union[PathLike, str],
    charset: Optional[str] = None,
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by randomly introducing typos.
    Potential typos are sourced from a Common Locale Data Repository (CLDR) keymap.
    Any character may be replaced with one of its horizontal or vertical neighbors on a keyboard.
    They may also be replaced with its upper- or lowercase variant.
    It is possible for a string to not be modified if a selected character has no possible replacements.

    :param cldr_path: path to CLDR keymap file
    :param charset: optional string with characters that may be corrupted (default: all characters)
    :param rng: random number generator to use (default: None)
    :return: function returning Pandas series of strings with random typos
    """
    if rng is None:
        rng = np.random.default_rng()

    with Path(cldr_path).open(mode="r", encoding="utf-8") as f:
        tree = etree.parse(f)

    root = tree.getroot()

    # compute the row and column count
    max_row, max_col = 0, 0

    for map_node in root.iterfind("./keyMap/map"):
        # decode_iso_kb_pos is cached so calling this repeatedly shouldn't have an impact on performance
        kb_row, kb_col = decode_iso_kb_pos(map_node.get("iso"))
        max_row = max(max_row, kb_row)
        max_col = max(max_col, kb_col)

    kb_map = np.chararray(
        shape=(
            max_row + 1,
            max_col + 1,
            2,
        ),  # + 1 because rows and cols are zero-indexed, 2 to accommodate shift
        itemsize=1,  # each cell holds one unicode char
        unicode=True,
    )
    kb_map[:] = ""  # initialize with empty strings

    # remember the kb pos for each character
    kb_char_to_kb_pos_dict: dict[str, (int, int, int)] = {}

    for key_map_node in root.iterfind("./keyMap"):
        key_map_mod = key_map_node.get("modifiers")

        if key_map_mod is None:
            kb_mod = 0
        elif key_map_mod == "shift":
            kb_mod = 1
        else:
            continue

        for map_node in key_map_node.iterfind("./map"):
            kb_row, kb_col = decode_iso_kb_pos(map_node.get("iso"))
            kb_char = unescape_kb_char(map_node.get("to"))

            # check that char is listed if charset of permitted chars is provided
            if charset is not None and kb_char not in charset:
                continue

            kb_char_to_kb_pos_dict[kb_char] = (kb_row, kb_col, kb_mod)
            kb_map[kb_row, kb_col, kb_mod] = kb_char

    # map each character with other nearby characters that it could be replaced with due to a typo
    kb_char_to_candidates_dict: dict[str, str] = {}

    with np.nditer(kb_map, flags=["multi_index"], op_flags=[["readonly"]]) as it:
        for kb_char in it:
            # iterator returns str as array of unicode chars. convert it to str.
            kb_char = str(kb_char)

            # skip keys that don't have a character assigned to them
            if kb_char == "":
                continue

            kb_pos = it.multi_index
            # noinspection PyTypeChecker
            kb_pos_neighbors = get_neighbor_kb_pos_for(kb_pos, max_row, max_col)
            kb_char_candidates = set()

            for kb_pos_neighbor in kb_pos_neighbors:
                kb_char_candidate = kb_map[kb_pos_neighbor]

                # check that the key pos has a char assigned to it. it may also happen that the char is the same
                # despite the kb modifier. that needs to be accounted for.
                if kb_char_candidate != "" and kb_char_candidate != kb_char:
                    kb_char_candidates.add(kb_char_candidate)

            # check that there are any candidates
            if len(kb_char_candidates) > 0:
                kb_char_to_candidates_dict[kb_char] = "".join(
                    sorted(
                        kb_char_candidates
                    )  # needs to be sorted to ensure reproducibility
                )

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        srs_str_out = srs_str_in.copy()
        str_count = len(srs_str_out)

        # string length series
        srs_str_out_len = srs_str_out.str.len()
        # random indices
        arr_rng_vals = rng.random(size=str_count)
        arr_rng_typo_indices = np.floor(srs_str_out_len * arr_rng_vals).astype(int)

        # create a new series containing the chars that have been randomly selected for replacement
        srs_typo_chars = pd.Series(dtype=str, index=srs_str_out.index)
        arr_uniq_idx = arr_rng_typo_indices.unique()

        for i in arr_uniq_idx:
            idx_mask = arr_rng_typo_indices == i
            srs_typo_chars[idx_mask] = srs_str_out[idx_mask].str[i]

        # create a new series that will track the replacement chars for the selected chars
        srs_repl_chars = pd.Series(dtype=str, index=srs_str_out.index)
        arr_uniq_chars = srs_typo_chars.unique()

        for char in arr_uniq_chars:
            # check if there are any possible replacements for this char
            if char not in kb_char_to_candidates_dict:
                continue

            # get candidate strings
            char_candidates = kb_char_to_candidates_dict[char]
            # count the rows that have this character selected
            char_count = (srs_typo_chars == char).sum()
            # draw replacements for the current character
            rand_chars = rng.choice(list(char_candidates), size=char_count)
            srs_repl_chars[srs_typo_chars == char] = rand_chars

        for i in arr_uniq_idx:
            # there is a possibility that a char might not have a replacement, so pd.notna() will have to
            # act as an extra filter to not modify strings that have no replacement
            idx_mask = (arr_rng_typo_indices == i) & pd.notna(srs_repl_chars)
            srs_str_out[idx_mask] = (
                srs_str_out[idx_mask].str[:i]
                + srs_repl_chars[idx_mask]
                + srs_str_out[idx_mask].str[i + 1 :]
            )

        return srs_str_out

    return _corrupt


def with_phonetic_replacement_table(
    csv_file_path: Union[PathLike, str],
    header: bool = False,
    source_column: Union[int, str] = 0,
    target_column: Union[int, str] = 1,
    flags_column: Union[int, str] = 2,
    encoding: str = "utf-8",
    delimiter: str = ",",
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by randomly replacing characters with others that sound similar.
    The rules for similar-sounding character sequences are sourced from a CSV file.
    This table must have at least three columns: a source, target and a flag column.
    A source pattern is mapped to its target under the rules imposed by the provided flags.
    These flags determine where such a replacement can take place within a string.
    If no flags are defined, it is implied that this replacement can take place anywhere in a string.
    Conversely, if `^`, `$`, `_`, or any combination of the three are set, it implies that a replacement
    can only occur at the start, end or in the middle of a string.

    :param csv_file_path: path to CSV file with pattern, replacement and flag column
    :param header: `True` if the file contains a header, `False` otherwise (default: `False`)
    :param encoding: character encoding of the CSV file (default: `utf-8`)
    :param delimiter: column delimiter (default: `,`)
    :param source_column: name of the source column if the file contains a header, otherwise the column index (default: `0`)
    :param target_column: name of the target column if the file contains a header, otherwise the column index (default: `1`)
    :param flags_column: name of the flags column if the file contains a header, otherwise the column index (default: `2`)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with phonetically similar replacements
    """
    # list of all flags. needs to be sorted for rng.
    _all_flags = "".join(sorted("^$_"))

    def _validate_flags(flags_str: Optional[str]) -> str:
        """Check a string for valid flags. Returns all flags if string is empty, `NaN` or `None`."""
        if pd.isna(flags_str) or flags_str == "" or flags_str is None:
            return _all_flags

        for char in flags_str:
            if char not in _all_flags:
                raise ValueError(f"unknown flag: {char}")

        return flags_str

    def _raise_unknown_flag(flag: str) -> NoReturn:
        raise ValueError(f"invalid state: unknown flag `{flag}`")

    if rng is None:
        rng = np.random.default_rng()

    # read csv file
    df = pd.read_csv(
        csv_file_path,
        header=0 if header else None,
        dtype=str,
        usecols=[source_column, target_column, flags_column],
        sep=delimiter,
        encoding=encoding,
    )

    # parse replacement rules
    phonetic_replacement_rules: list[_PhoneticReplacementRule] = []

    for _, row in df.iterrows():
        pattern = row[source_column]
        replacement = row[target_column]
        flags = _validate_flags(row[flags_column])

        phonetic_replacement_rules.append(
            _PhoneticReplacementRule(pattern, replacement, flags)
        )

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        # create a copy of input series
        srs_str_out = srs_str_in.copy()
        # get series of string lengths
        srs_str_out_len = srs_str_out.str.len()
        # get series length
        str_count = len(srs_str_out)
        # create series to compute substitution probability
        srs_str_sub_prob = pd.Series(dtype=float, index=srs_str_out.index)
        srs_str_sub_prob[:] = 0
        # track possible replacements for each rule
        rule_to_flag_dict: dict[_PhoneticReplacementRule, pd.Series] = {}

        for rule in phonetic_replacement_rules:
            # increment absolute frequency for each string where rule applies
            srs_str_flags = pd.Series(dtype=str, index=srs_str_out.index)
            srs_str_flags[:] = ""

            # find pattern in series
            srs_pattern_idx = srs_str_out.str.find(rule.pattern)

            if "^" in rule.flags:
                # increment counter for all strings where pattern is found at start of string
                mask_pattern_at_start = srs_pattern_idx == 0
                srs_str_flags[mask_pattern_at_start] += "^"

            if "$" in rule.flags:
                # increment counter for all strings where pattern is found at end of string
                mask_pattern_at_end = (
                    srs_pattern_idx + len(rule.pattern) == srs_str_out_len
                )
                srs_str_flags[mask_pattern_at_end] += "$"

            if "_" in rule.flags:
                # increment counter for all strings where pattern is not at the start and at the end
                mask_pattern_in_middle = (srs_pattern_idx > 0) & (
                    srs_pattern_idx + len(rule.pattern) < srs_str_out_len
                )
                srs_str_flags[mask_pattern_in_middle] += "_"

            rule_to_flag_dict[rule] = srs_str_flags
            srs_str_sub_prob[srs_str_flags != ""] += 1

        # prevent division by zero
        mask_eligible_strs = srs_str_sub_prob != 0
        # absolute -> relative frequency
        srs_str_sub_prob[mask_eligible_strs] = 1 / srs_str_sub_prob[mask_eligible_strs]
        # keep track of modified rows
        mask_modified_rows = pd.Series(dtype=bool, index=srs_str_out.index)
        mask_modified_rows[:] = False

        for rule in phonetic_replacement_rules:
            # draw random numbers for each row
            arr_rand_vals = rng.random(size=str_count)
            # get flags that were generated for each row
            srs_str_flags = rule_to_flag_dict[rule]
            # get candidate row mask
            mask_candidate_rows = (arr_rand_vals < srs_str_sub_prob) & (
                srs_str_flags != ""
            )

            # create copy of rule flags and shuffle it in-place
            arr_rand_flags = list(rule.flags)
            rng.shuffle(arr_rand_flags)

            for flag in arr_rand_flags:
                # select rows that can have the current rule applied to them, fit into the correct flag
                # and haven't been modified yet
                if flag == "^":
                    mask_current_flag = srs_str_out.str.startswith(rule.pattern)
                elif flag == "$":
                    mask_current_flag = srs_str_out.str.endswith(rule.pattern)
                elif flag == "_":
                    # not at the start and not at the end
                    mask_current_flag = ~srs_str_out.str.startswith(rule.pattern) & (
                        ~srs_str_out.str.endswith(rule.pattern)
                    )
                else:
                    _raise_unknown_flag(flag)

                mask_current_candidate_rows = (
                    mask_candidate_rows & mask_current_flag & ~mask_modified_rows
                )

                # skip if there are no replacements to be made
                if mask_current_candidate_rows.sum() == 0:
                    continue

                if flag == "^":
                    srs_str_out[mask_current_candidate_rows] = srs_str_out[
                        mask_current_candidate_rows
                    ].str.replace(f"^{rule.pattern}", rule.replacement, n=1, regex=True)
                elif flag == "$":
                    srs_str_out[mask_current_candidate_rows] = srs_str_out[
                        mask_current_candidate_rows
                    ].str.replace(f"{rule.pattern}$", rule.replacement, n=1, regex=True)
                elif flag == "_":
                    # matching groups are the parts that are supposed to be preserved
                    # (anything but the string to replace).
                    srs_str_out[mask_current_candidate_rows] = srs_str_out[
                        mask_current_candidate_rows
                    ].str.replace(
                        f"^(.+){rule.pattern}(.+)$",
                        f"\\1{rule.replacement}\\2",
                        n=1,
                        regex=True,
                    )
                else:
                    _raise_unknown_flag(flag)

                # update modified row series
                mask_modified_rows |= mask_current_candidate_rows

        return srs_str_out

    return _corrupt


def with_replacement_table(
    csv_file_path: Union[PathLike, str],
    header: bool = False,
    source_column: Union[str, int] = 0,
    target_column: Union[str, int] = 1,
    encoding: str = "utf-8",
    delimiter: str = ",",
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by randomly substituting sequences from a replacement table.
    The table must have at least two columns: a source and a target value column.
    A source value may have multiple target values that it can map to.
    Strings that do not contain any possible source values are not corrupted.
    It is possible for a string to not be modified if no target value could be picked for its assigned source value.
    This can only happen if a source value is mapped to multiple target values.
    In this case, each target value will be independently selected or not.

    :param csv_file_path: path to CSV file with source and target column
    :param header: `True` if the file contains a header, `False` otherwise (default: `False`)
    :param source_column: name of the source column if the file contains a header, otherwise the column index (default: `0`)
    :param target_column: name of the target column if the file contains a header, otherwise the column index (default: `1`)
    :param encoding: character encoding of the CSV file (default: `utf-8`)
    :param delimiter: column delimiter (default: `,`)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with inline substitutions according to replacement table
    """
    if rng is None:
        rng = np.random.default_rng()

    if header and (isinstance(source_column, str) or isinstance(target_column, str)):
        raise ValueError(
            "header present, but source and target columns must be strings"
        )

    df = pd.read_csv(
        csv_file_path,
        header=0 if header else None,
        dtype=str,
        usecols=[source_column, target_column],
        sep=delimiter,
        encoding=encoding,
    )

    srs_unique_source_values = df[source_column].unique()

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        # create copy of input series
        srs_str_out = srs_str_in.copy()
        str_count = len(srs_str_out)
        # create series to compute probability of substitution for each row
        srs_str_sub_prob = pd.Series(dtype=float, index=srs_str_out.index)
        srs_str_sub_prob[:] = 0

        for source in srs_unique_source_values:
            # increment absolute frequency for each string containing source value by one
            srs_str_sub_prob[srs_str_out.str.contains(source)] += 1

        # prevent division by zero
        mask_eligible_strs = srs_str_sub_prob != 0
        # convert absolute frequencies into relative frequencies
        srs_str_sub_prob[mask_eligible_strs] = 1 / srs_str_sub_prob[mask_eligible_strs]

        # create dataframe to track source and target for each row
        df_replacement = pd.DataFrame(
            index=srs_str_out.index, columns=["source", "target"], dtype=str
        )

        for source in srs_unique_source_values:
            # select all rows that contain the source value
            srs_str_contains_source = srs_str_out.str.contains(source)
            # draw random numbers for each row
            arr_rand_vals = rng.random(size=str_count)
            # select only rows that contain the source string, have a random number drawn that's
            # in range of its probability to be modified, and hasn't been marked for replacement yet
            mask_strings_to_replace = (
                srs_str_contains_source
                & (arr_rand_vals < srs_str_sub_prob)
                & pd.isna(df_replacement["source"])
            )
            # count all strings that meet the conditions above
            replacement_count = mask_strings_to_replace.sum()

            # skip if there are no replacements to be made
            if replacement_count == 0:
                continue

            # fill in the source column of the replacement table
            df_replacement.loc[mask_strings_to_replace, "source"] = source

            # select all target values that can be generated from the current source value
            replacement_options = df[df[source_column] == source][
                target_column
            ].tolist()

            # trivial case
            if len(replacement_options) == 1:
                target = replacement_options[0]
                df_replacement.loc[mask_strings_to_replace, "target"] = target
                continue

            # otherwise draw a random target value for each row
            df_replacement.loc[mask_strings_to_replace, "target"] = rng.choice(
                replacement_options, size=replacement_count
            )

        # iterate over all unique source values
        for source in df_replacement["source"].unique():
            # skip nan
            if pd.isna(source):
                continue

            # for each unique source value, iterate over its unique target values
            for target in df_replacement[df_replacement["source"] == source][
                "target"
            ].unique():
                # select all rows that have this specific source -> target replacement going
                mask = (df_replacement["source"] == source) & (
                    df_replacement["target"] == target
                )

                # perform replacement of source -> target
                srs_str_out[mask] = srs_str_out[mask].str.replace(source, target, n=1)

        return srs_str_out

    return _corrupt


def _corrupt_all_from_value(value: str) -> Corruptor:
    """
    Corrupt a series of strings by replacing all of its values with the same "missing" value.

    :param value: "missing" value to replace entries with
    :return: function returning Pandas series where all entries are replaced with "missing" value
    """

    def _corrupt_list(str_in_srs: pd.Series) -> pd.Series:
        return pd.Series(
            data=[value] * len(str_in_srs),
            index=str_in_srs.index,
            dtype=str,
        )

    return _corrupt_list


def _corrupt_only_empty_from_value(value: str) -> Corruptor:
    """
    Corrupt a series of strings by replacing all of its empty values (string length = 0) with the
    same "missing" value.

    :param value: "missing" value to replace empty entries with
    :return: function returning Pandas series where all empty entries are replaced with "missing" value
    """

    def _corrupt_list(str_in_srs: pd.Series) -> pd.Series:
        str_out_srs = str_in_srs.copy()
        str_out_srs[str_out_srs == ""] = value
        return str_out_srs

    return _corrupt_list


def _corrupt_only_blank_from_value(value: str) -> Corruptor:
    """
    Corrupt a series of strings by replacing all of its blank values (empty strings after trimming whitespaces)
    with the same "missing" value.

    :param value: "missing" value to replace blank entries with
    :return: function returning Pandas series where all blank entries are replaced with "missing" value
    """

    def _corrupt_list(str_in_srs: pd.Series) -> pd.Series:
        str_out_srs = str_in_srs.copy()
        str_out_srs[str_out_srs.str.strip() == ""] = value
        return str_out_srs

    return _corrupt_list


def with_missing_value(
    value: str = "",
    strategy: Literal["all", "blank", "empty"] = "blank",
) -> Corruptor:
    """
    Corrupt a series of strings by replacing select entries with a representative "missing" value.
    Strings are selected for replacement depending on the chosen strategy.
    If `all`, then all strings in the series will be replaced with the missing value.
    If `blank`, then all strings that are either empty or consist of whitespace characters only will be replaced with the missing value.
    If `empty`, then all strings that are empty will be replaced with the missing value.

    :param value: "missing" value to replace select entries with (default: empty string)
    :param strategy: `all`, `blank` or `empty` to select values to replace (default: `blank`)
    :return: function returning Pandas series of strings where select entries are replaced with a "missing" value
    """
    if strategy == "all":
        return _corrupt_all_from_value(value)
    elif strategy == "blank":
        return _corrupt_only_blank_from_value(value)
    elif strategy == "empty":
        return _corrupt_only_empty_from_value(value)
    else:
        raise ValueError(f"unrecognized replacement strategy: {strategy}")


def with_insert(
    charset: str = string.ascii_letters,
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by inserting random characters.
    The characters are drawn from the provided charset.

    :param charset: string to sample random characters from (default: all ASCII letters)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with randomly inserted characters
    """
    if rng is None:
        rng = np.random.default_rng()

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        srs_str_out = srs_str_in.copy()
        str_count = len(srs_str_out)

        # get series of lengths of all strings in series
        srs_str_out_len = srs_str_out.str.len()
        # draw random values
        arr_rng_vals = rng.random(size=str_count)
        # compute indices from random values (+1 because letters can be inserted at the ned)
        arr_rng_insert_indices = np.floor((srs_str_out_len + 1) * arr_rng_vals).astype(
            int
        )
        # generate random char for each string
        srs_rand_chars = pd.Series(
            rng.choice(list(charset), size=str_count),
            copy=False,  # use np array
            index=srs_str_out.index,  # align index
        )
        # determine all unique random indices
        arr_uniq_idx = arr_rng_insert_indices.unique()

        for i in arr_uniq_idx:
            # select all strings with the same random insert index
            srs_idx_mask = arr_rng_insert_indices == i
            # insert character at current index
            srs_str_out[srs_idx_mask] = (
                srs_str_out[srs_idx_mask].str[:i]
                + srs_rand_chars[srs_idx_mask]
                + srs_str_out[srs_idx_mask].str[i:]
            )

        return srs_str_out

    return _corrupt


def with_delete(rng: Optional[np.random.Generator] = None) -> Corruptor:
    """
    Corrupt a series of strings by randomly deleting characters.

    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with randomly deleted characters
    """
    if rng is None:
        rng = np.random.default_rng()

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        # get series of string lengths
        srs_str_out_len = srs_str_in.str.len()
        # limit view to strings that have at least one character
        srs_str_out_min_len = srs_str_in[srs_str_out_len >= 1]

        # check that there are any strings to modify
        if len(srs_str_out_min_len) == 0:
            return srs_str_in

        # create copy after length check
        srs_str_out = srs_str_in.copy()
        # generate random indices
        arr_rng_vals = rng.random(size=len(srs_str_out_min_len))
        arr_rng_delete_indices = np.floor(
            srs_str_out_min_len.str.len() * arr_rng_vals
        ).astype(int)
        # determine unique indices
        arr_uniq_idx = arr_rng_delete_indices.unique()

        for i in arr_uniq_idx:
            # select all strings with the same random delete index
            srs_idx_mask = arr_rng_delete_indices == i
            # delete character at selected index
            srs_str_out.update(
                srs_str_out_min_len[srs_idx_mask].str.slice_replace(i, i + 1, "")
            )

        return srs_str_out

    return _corrupt


def with_transpose(rng: Optional[np.random.Generator] = None) -> Corruptor:
    """
    Corrupt a series of strings by randomly swapping neighboring characters.
    Note that it is possible for the same two neighboring characters to be swapped.

    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with randomly swapped neighboring characters
    """
    if rng is None:
        rng = np.random.default_rng()

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        # length of strings
        srs_str_out_len = srs_str_in.str.len()
        # limit view to strings that have at least two characters
        srs_str_out_min_len = srs_str_in[srs_str_out_len >= 2]

        # check that there are any strings to modify
        if len(srs_str_out_min_len) == 0:
            return srs_str_in

        # create a copy only after running the length check
        srs_str_out = srs_str_in.copy()
        # generate random numbers
        arr_rng_vals = rng.random(size=len(srs_str_out_min_len))

        # -1 as neighboring char can be transposed
        arr_rng_transpose_indices = np.floor(
            (srs_str_out_min_len.str.len() - 1) * arr_rng_vals
        ).astype(int)
        # unique indices
        arr_uniq_idx = arr_rng_transpose_indices.unique()

        for i in arr_uniq_idx:
            # select strings that have the same transposition
            srs_idx_mask = arr_rng_transpose_indices == i
            srs_masked = srs_str_out_min_len[srs_idx_mask]
            srs_str_out.update(
                srs_masked.str[:i]
                + srs_masked.str[i + 1]
                + srs_masked.str[i]
                + srs_masked.str[i + 2 :]
            )

        return srs_str_out

    return _corrupt


def with_substitute(
    charset: str = string.ascii_letters,
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by replacing single characters with a new one.
    The characters are drawn from the provided charset.
    Note that it is possible for a character to be replaced by itself.

    :param charset: string to sample random characters from (default: all ASCII letters)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with randomly inserted characters
    """
    if rng is None:
        rng = np.random.default_rng()

    def _corrupt(srs_str_in: pd.Series) -> pd.Series:
        # length of strings
        srs_str_out_len = srs_str_in.str.len()
        # limit view to strings that have at least 1 character
        srs_str_out_min_len = srs_str_in[srs_str_out_len >= 1]

        # check that there are any strings to modify
        if len(srs_str_out_min_len) == 0:
            return srs_str_in

        # create copy after length check
        srs_str_out = srs_str_in.copy()
        # count strings that may be modified
        str_count = len(srs_str_out_min_len)
        # random indices
        arr_rng_vals = rng.random(size=str_count)
        arr_rng_sub_indices = np.floor(
            srs_str_out_min_len.str.len() * arr_rng_vals
        ).astype(int)
        # random substitution chars
        srs_rand_chars = pd.Series(
            rng.choice(list(charset), size=str_count),
            copy=False,  # use np array
            index=srs_str_out_min_len.index,  # align index
        )
        arr_uniq_idx = arr_rng_sub_indices.unique()

        for i in arr_uniq_idx:
            srs_idx_mask = arr_rng_sub_indices == i
            srs_masked = srs_str_out_min_len[srs_idx_mask]
            srs_str_out.update(
                srs_masked.str[:i]
                + srs_rand_chars[srs_idx_mask]
                + srs_masked.str[i + 1 :]
            )

        return srs_str_out

    return _corrupt


def with_edit(
    p_insert: float = 0.25,
    p_delete: float = 0.25,
    p_substitute: float = 0.25,
    p_transpose: float = 0.25,
    charset: str = string.ascii_letters,
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by randomly applying insertion, deletion, substitution or transposition of characters.
    This corruptor works as a wrapper around the respective corruptors for the mentioned individual operations.
    The charset of allowed characters is passed on to the insertion and substitution corruptors.
    Each corruptor receives its own isolated RNG which is derived from the RNG passed into this function.
    The probabilities of each corruptor must sum up to 1.

    :param p_insert: probability of random character insertion on a string (default: `0.25`, 25%)
    :param p_delete: probability of random character deletion on a string (default: `0.25`, 25%)
    :param p_substitute: probability of random character substitution on a string (default: `0.25`, 25%)
    :param p_transpose: probability of random character transposition on a string (default: `0.25`, 25%)
    :param charset: string to sample random characters from for insertion and substitution (default: all ASCII letters)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings with randomly mutated characters
    """
    if rng is None:
        rng = np.random.default_rng()

    edit_ops: list[_EditOp] = ["ins", "del", "sub", "trs"]
    edit_ops_prob = [p_insert, p_delete, p_substitute, p_transpose]

    for p in edit_ops_prob:
        _check_probability_in_bounds(p)

    try:
        # sanity check
        rng.choice(edit_ops, p=edit_ops_prob)
    except ValueError:
        raise ValueError("probabilities must sum up to 1.0")

    # equip every corruptor with its own independent rng derived from this corruptor's rng
    rng_ins, rng_del, rng_sub, rng_trs = rng.spawn(4)
    corr_ins, corr_del, corr_sub, corr_trs = (
        with_insert(charset, rng_ins),
        with_delete(rng_del),
        with_substitute(charset, rng_sub),
        with_transpose(rng_trs),
    )

    def _corrupt_list(srs_in: pd.Series) -> pd.Series:
        srs_out = srs_in.copy()
        str_in_edit_ops = pd.Series(
            rng.choice(edit_ops, size=len(srs_in), p=edit_ops_prob),
            index=srs_in.index,
        )

        msk_ins = str_in_edit_ops == "ins"

        if msk_ins.sum() != 0:
            srs_out[msk_ins] = corr_ins(srs_out[msk_ins])

        msk_del = str_in_edit_ops == "del"

        if msk_del.sum() != 0:
            srs_out[msk_del] = corr_del(srs_out[msk_del])

        msk_sub = str_in_edit_ops == "sub"

        if msk_sub.sum() != 0:
            srs_out[msk_sub] = corr_sub(srs_out[msk_sub])

        msk_trs = str_in_edit_ops == "trs"

        if msk_trs.sum() != 0:
            srs_out[msk_trs] = corr_trs(srs_out[msk_trs])

        return srs_out

    return _corrupt_list


def with_noop() -> Corruptor:
    """
    Corrupt a series by not corrupting it at all.
    This corruptor returns the input series as-is.
    You might use it to leave a certain percentage of records in a series untouched.

    :return: function returning Pandas series as-is
    """

    def _corrupt(srs_in: pd.Series) -> pd.Series:
        return srs_in

    return _corrupt


def with_categorical_values(
    csv_file_path: Union[PathLike, str],
    header: bool = False,
    value_column: Union[str, int] = 0,
    encoding: str = "utf-8",
    delimiter: str = ",",
    rng: Optional[np.random.Generator] = None,
) -> Corruptor:
    """
    Corrupt a series of strings by replacing it with another from a list of categorical values.
    This corruptor reads all unique values from a column within a CSV file.
    All strings within a series will be replaced with a different random value from this column.

    :param csv_file_path: CSV file to read from
    :param header: `True` if the file contains a header, `False` otherwise (default: `False`)
    :param value_column: name of column with categorical values if the file contains a header, otherwise the column index (default: `0`)
    :param encoding: character encoding of the CSV file (default: `utf-8`)
    :param delimiter: column delimiter (default: `,`)
    :param rng: random number generator to use (default: `None`)
    :return: function returning Pandas series of strings that are replaced with a different value from a category
    """
    if rng is None:
        rng = np.random.default_rng()

    if header and not isinstance(value_column, str):
        raise ValueError("header present, but value column must be a string")

    # read csv file
    df = pd.read_csv(
        csv_file_path,
        header=0 if header else None,
        dtype=str,
        usecols=[value_column],
        sep=delimiter,
        encoding=encoding,
    )

    # fetch unique values
    unique_values = pd.Series(df[value_column].dropna().unique())

    def _corrupt_list(srs_in: pd.Series) -> pd.Series:
        nonlocal unique_values

        # create a new series with which the original one will be updated.
        # for starters all rows will be NaN. dtype is to avoid typecast warning.
        srs_in_update = pd.Series(
            np.full(len(srs_in), np.nan), copy=False, dtype=str, index=srs_in.index
        )

        for unique_val in unique_values:
            # remove current value from list of unique values
            unique_vals_without_current = np.setdiff1d(unique_values, unique_val)
            # select all rows that equal the current value
            srs_in_matching_val = srs_in.str.fullmatch(unique_val)
            # count the rows that contain the current value
            unique_val_total = srs_in_matching_val.sum()

            # skip if there are no values to generate
            if unique_val_total == 0:
                continue

            # draw from the list of values excluding the current one
            new_unique_vals = rng.choice(
                unique_vals_without_current, size=unique_val_total
            )

            # populate the series that is used for updating the original one
            srs_in_update[srs_in_matching_val] = new_unique_vals

        # update() is performed in-place, so create a copy of the initial series first.
        srs_out = srs_in.copy()
        srs_out.update(srs_in_update)

        return srs_out

    return _corrupt_list


def corrupt_dataframe(
    df_in: pd.DataFrame,
    column_to_corruptor_dict: dict[
        str,
        Union[Corruptor, list[Corruptor], list[tuple[float, Corruptor]]],
    ],
    rng: Optional[np.random.Generator] = None,
):
    """
    Corrupt a dataframe by applying several corruptors on select columns.
    This function takes a dictionary which has column names as keys and corruptors as values.
    A column may be assigned a single corruptor, a list of corruptors where each is applied with the same
    probability, and a list of weighted corruptors where each is applied with its assigned probability.

    :param df_in: dataframe to corrupt
    :param column_to_corruptor_dict: dictionary of columns to corruptors
    :param rng: random number generator to use (default: `None`)
    :return: copy of dataframe with corruptors applied as specified
    """
    if rng is None:
        rng = np.random.default_rng()

    df_out = df_in.copy()

    for column, corruptor_spec in column_to_corruptor_dict.items():
        if column not in df_in.columns:
            raise ValueError(
                f"column `{column}` does not exist, must be one of `{','.join(df_in.columns)}`"
            )

        # if the column contains only a single corruptor, assign it with a probability of 1.0
        if not isinstance(corruptor_spec, list):
            corruptor_spec = [(1.0, corruptor_spec)]

        # if the list contains functions only, create them into tuples with equal probability
        if type(corruptor_spec[0]) is not tuple:
            corruptor_spec = [
                (1.0 / len(corruptor_spec), corruptor) for corruptor in corruptor_spec
            ]

        # corruptor_spec is a list of tuples, which contain a float and a corruptor func.
        # this one-liner collects all floats and corruptor funcs into their own lists.
        p_values, corruptor_funcs = list(zip(*corruptor_spec))

        try:
            # sanity check
            rng.choice([i for i in range(len(p_values))], p=p_values)
        except ValueError:
            raise ValueError(f"probabilities for column `{column}` must sum up to 1.0")

        corruptor_count = len(corruptor_funcs)
        # generate a series where each row gets an index of the corruptor in corruptor_funcs to apply.
        arr_corruptor_idx = np.arange(corruptor_count)
        arr_corruptor_per_row = rng.choice(
            arr_corruptor_idx, p=p_values, size=len(df_out)
        )
        srs_corruptor_idx = pd.Series(data=arr_corruptor_per_row, index=df_out.index)
        srs_column = df_in[column]

        for i in arr_corruptor_idx:
            corruptor = corruptor_funcs[i]
            mask_this_corruptor = srs_corruptor_idx == i
            df_out.loc[mask_this_corruptor, column] = corruptor(
                srs_column[mask_this_corruptor]
            )

    return df_out