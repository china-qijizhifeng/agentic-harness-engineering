# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import mmap
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any, cast

import pandas as pd
import yaml

PD_ANY = cast(Any, pd)

LLM_FRIENDLY_MAX_DEPTH = int(os.getenv("LLM_FRIENDLY_MAX_DEPTH", 5))
LLM_FRIENDLY_MAX_SIZE_IN_BYTES = int(
    os.getenv(
        "LLM_FRIENDLY_MAX_SIZE_IN_BYTES",
        5000,
    )
)
LLM_FRIENDLY_MAX_ARRAY_LENGTH = int(os.getenv("LLM_FRIENDLY_MAX_ARRAY_LENGTH", 10))
LLM_FRIENDLY_MAX_DICT_KEYS = int(os.getenv("LLM_FRIENDLY_MAX_DICT_KEYS", 10))
LLM_FRIENDLY_MAX_STRING_LENGTH = int(
    os.getenv(
        "LLM_FRIENDLY_MAX_STRING_LENGTH",
        200,
    )
)
LLM_FRIENDLY_MAX_SAMPLE_ROWS = int(os.getenv("LLM_FRIENDLY_MAX_SAMPLE_ROWS", 10))


class ContentWithCustomLength:
    def __init__(self, content: str | list[Any], custom_length: int):
        self.content = content
        self.custom_length = custom_length

    def __len__(self):
        return self.custom_length


class DataFrameFormatter:
    """
    Utility class for formatting DataFrames with type information for LLM consumption.
    """

    @staticmethod
    def format_dataframe_with_types(
        df: pd.DataFrame,
        sample_rows: int = 5,
    ) -> dict[str, Any]:
        """
        Format DataFrame with detailed type information for each column.

        Args:
            df: DataFrame to format
            sample_rows: Number of sample rows to include

        Returns:
            Dictionary with DataFrame info and sample data
        """
        # Get basic info
        info: dict[str, Any] = {
            "shape": list(df.shape),
            "columns": list(df.columns),
            "dtypes": {},
            "non_null_counts": {},
            "sample_data": {},
            "memory_usage_mb": round(df.memory_usage(deep=True).sum() / 1024 / 1024, 2),
        }

        # Get detailed type information for each column
        for col in df.columns:
            dtype = df[col].dtype
            info["dtypes"][col] = {
                "pandas_type": str(dtype),
                "python_type": DataFrameFormatter._infer_python_type(df[col]),
            }

            # Add unique value count for categorical-like columns
            unique_count = df[col].nunique()
            if unique_count < 20:  # Likely categorical
                info["dtypes"][col]["unique_values"] = sorted(
                    df[col].dropna().unique().tolist(),
                )
            else:
                info["dtypes"][col]["unique_count"] = unique_count

            # Non-null count
            info["non_null_counts"][col] = int(df[col].count())

            # Sample values
            sample_values = df[col].dropna().head(sample_rows).tolist()
            info["sample_data"][col] = sample_values

        return info

    @staticmethod
    def _infer_python_type(series: pd.Series) -> str:
        """
        Infer the most appropriate Python type for a pandas Series.
        """
        dtype = series.dtype

        if pd.api.types.is_integer_dtype(dtype):
            return "int"
        elif pd.api.types.is_float_dtype(dtype):
            return "float"
        elif pd.api.types.is_bool_dtype(dtype):
            return "bool"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            return "datetime"
        elif pd.api.types.is_timedelta64_dtype(dtype):
            return "timedelta"
        elif isinstance(dtype, pd.CategoricalDtype):
            return "category"
        else:
            return "str"


class FileFormatHandler:
    """
    Handler for different file formats with appropriate parsing methods.
    """

    @staticmethod
    def read_json(path: str) -> Any:
        """Read JSON files"""
        with open(path, encoding="utf-8") as file:
            return json.load(file)

    @staticmethod
    def read_jsonl(path: str) -> list[Any] | ContentWithCustomLength:
        """Read JSONL files"""
        line_nums = FileFormatHandler._get_file_line_nums(path)
        if line_nums <= LLM_FRIENDLY_MAX_ARRAY_LENGTH * 2:
            with open(path, encoding="utf-8") as file:
                return [json.loads(line) for line in file if line.strip()]
        else:
            content: list[Any] = []
            with open(path, "rb") as raw_file:
                mapped_file = mmap.mmap(raw_file.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    for idx, line in enumerate(iter(mapped_file.readline, b"")):
                        if idx >= LLM_FRIENDLY_MAX_ARRAY_LENGTH * 2:
                            break
                        content.append(json.loads(line.decode("utf-8")))
                finally:
                    mapped_file.close()
            return ContentWithCustomLength(content, line_nums)

    @staticmethod
    def read_csv(path: str) -> pd.DataFrame:
        """Read CSV files"""
        return cast(pd.DataFrame, PD_ANY.read_csv(path))

    @staticmethod
    def read_tsv(path: str) -> pd.DataFrame:
        """Read TSV (Tab-Separated Values) files"""
        return cast(pd.DataFrame, PD_ANY.read_csv(path, sep="\t"))

    @staticmethod
    def read_excel(path: str) -> pd.DataFrame:
        """Read Excel files (.xlsx, .xls)"""
        try:
            df = cast(pd.DataFrame, PD_ANY.read_excel(path))
            return df
        except Exception as e:
            raise Exception(f"Failed to read Excel file: {str(e)}")

    @staticmethod
    def read_parquet(path: str) -> pd.DataFrame:
        """Read Parquet files"""
        return cast(pd.DataFrame, PD_ANY.read_parquet(path))

    @staticmethod
    def read_yaml(path: str) -> Any:
        """Read YAML files"""
        with open(path, encoding="utf-8") as file:
            return yaml.safe_load(file)

    @staticmethod
    def read_xml(path: str) -> dict[str, Any]:
        """Read XML files and convert to structured format"""
        tree = ET.parse(path)
        root = tree.getroot()
        return FileFormatHandler._xml_to_dict(root)

    @staticmethod
    def _xml_to_dict(element: ET.Element) -> dict[str, Any]:
        """Convert XML element to dictionary"""
        result: dict[str, Any] = {}

        # Add attributes
        if element.attrib:
            result["@attributes"] = element.attrib

        # Add text content
        if element.text and element.text.strip():
            result["#text"] = element.text.strip()

        # Add children
        children: dict[str, Any] = {}
        for child in element:
            child_data = FileFormatHandler._xml_to_dict(child)
            if child.tag in children:
                if not isinstance(children[child.tag], list):
                    children[child.tag] = [children[child.tag]]
                children[child.tag].append(child_data)
            else:
                children[child.tag] = child_data

        result.update(children)
        return result

    @staticmethod
    def read_text(path: str) -> str | ContentWithCustomLength:
        """Read text files"""
        file_size = FileFormatHandler._get_file_size(path)
        if file_size <= LLM_FRIENDLY_MAX_SIZE_IN_BYTES * 2:
            with open(path, encoding="utf-8") as file:
                return file.read()
        else:
            with open(path, "rb") as raw_file:
                mapped_file = mmap.mmap(raw_file.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    content = mapped_file.read(LLM_FRIENDLY_MAX_SIZE_IN_BYTES * 2).decode(
                        "utf-8",
                        errors="replace",
                    )
                finally:
                    mapped_file.close()

            avg_bytes_per_char = len(content) / len(content.encode("utf-8"))
            estimated_char_count = int(
                file_size * avg_bytes_per_char,
            )  # Estimated! Not accurate!

            return ContentWithCustomLength(content, estimated_char_count)

    @staticmethod
    def _get_file_line_nums(path: str) -> int:
        """Get the number of lines in a file"""
        try:
            # Try using wc command first (faster for large files)
            return int(
                subprocess.check_output(
                    ["wc", "-l", path],
                )
                .decode("utf-8")
                .split()[0],
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to Python method if wc command fails
            with open(path, "rb") as f:
                lines = 0
                buf_size = 1024 * 1024
                read_f = f.read
                buf = read_f(buf_size)
                while buf:
                    lines += buf.count(b"\n")
                    buf = read_f(buf_size)
                return lines

    @staticmethod
    def _get_file_size(path: str) -> int:
        """Get the size of a file in bytes"""
        try:
            # Try using du command first (may be more accurate for some file systems)
            return int(
                subprocess.check_output(
                    ["du", "-b", path],
                )
                .decode("utf-8")
                .split()[0],
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to Python method for cross-platform compatibility
            return os.path.getsize(path)


class DataNormalizer:
    @staticmethod
    def normalize_to_size(
        obj: Any,
        max_depth: int = 3,
        max_size_in_bytes: int = 100_000,
    ) -> Any:
        """
        Normalize object to fit within specified size constraints.

        Args:
            obj: Object to normalize
            max_depth: Maximum depth for normalization
            max_size_in_bytes: Maximum size in bytes

        Returns:
            Normalized object
        """
        # First attempt at full depth
        normalized = DataNormalizer.normalize(obj, max_depth)
        size = DataNormalizer.estimate_size(normalized)

        # Iteratively reduce depth until size fits
        while size > max_size_in_bytes and max_depth > 0:
            max_depth -= 1
            normalized = DataNormalizer.normalize(obj, max_depth)
            size = DataNormalizer.estimate_size(normalized)

        return normalized

    @staticmethod
    def normalize(
        obj: Any,
        max_depth: int,
        current_depth: int = 0,
        visited: set[int] | None = None,
    ) -> Any:
        """
        Normalize an object by converting complex types to serializable forms.

        Args:
            obj: Object to normalize
            max_depth: Maximum depth for normalization
            current_depth: Current depth in recursion
            visited: Set of visited object IDs to detect circular references

        Returns:
            Normalized object
        """
        if visited is None:
            visited = set()

        if hasattr(obj, "__len__"):
            length = len(obj)
        else:
            length = None
        if isinstance(obj, ContentWithCustomLength):
            obj = obj.content

        # Handle functions
        if callable(obj) and not isinstance(obj, type):
            return f"[Function: {getattr(obj, '__name__', 'anonymous')}]"

        # Primitives pass through
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj

        # Depth limit reached
        if current_depth >= max_depth:
            if isinstance(obj, list):
                return f"[Array({length})]"
            if isinstance(obj, str):
                length = len(obj)
                if length > LLM_FRIENDLY_MAX_STRING_LENGTH:
                    return f"{obj[:LLM_FRIENDLY_MAX_STRING_LENGTH]}...{length - LLM_FRIENDLY_MAX_STRING_LENGTH} more characters"
                else:
                    return obj
            return f"[{type(obj).__name__}]"

        # Circular reference detection
        obj_id = id(obj)
        if obj_id in visited:
            return "[Circular]"
        visited.add(obj_id)

        try:
            # Special handling for known types
            if isinstance(obj, Exception):
                exception_payload: dict[str, Any] = {
                    "name": obj.__class__.__name__,
                    "message": str(obj),
                }
                if hasattr(obj, "__traceback__") and obj.__traceback__:
                    exception_payload["stack"] = DataNormalizer._truncate_stack(
                        DataNormalizer._format_traceback(obj.__traceback__),
                    )
                return exception_payload

            if isinstance(obj, datetime):
                return obj.isoformat()

            if isinstance(obj, re.Pattern):
                pattern = str(cast(Any, obj).pattern)
                flags = obj.flags
                return f"/{pattern}/{flags}"

            if isinstance(obj, Decimal):
                return str(obj)

            # Handle objects with toJSON-like methods
            to_dict_fn = getattr(obj, "to_dict", None)
            if callable(to_dict_fn):
                try:
                    return DataNormalizer.normalize(
                        to_dict_fn(),
                        max_depth,
                        current_depth,
                        visited,
                    )
                except Exception:
                    return "[Object with to_dict error]"

            if hasattr(obj, "__dict__"):
                # Handle custom objects
                object_result: dict[str, Any] = {}
                obj_dict = obj.__dict__
                keys = list(obj_dict.keys())
                max_props = LLM_FRIENDLY_MAX_DICT_KEYS

                # Respect normalization directives
                if hasattr(obj, "__sentry_skip_normalization__"):
                    return obj

                effective_max_depth = getattr(
                    obj,
                    "__sentry_override_normalization_depth__",
                    max_depth,
                )

                for _, key in enumerate(keys[:max_props]):
                    try:
                        object_result[key] = DataNormalizer.normalize(
                            obj_dict[key],
                            effective_max_depth,
                            current_depth + 1,
                            visited,
                        )
                    except Exception:
                        object_result[key] = "[Error accessing property]"

                if len(keys) > max_props:
                    object_result["..."] = f"{len(keys) - max_props} more properties"

                return object_result

            # Handle lists
            if isinstance(obj, (list, tuple, set)):
                sequence_items: list[Any] = list(cast(Iterable[Any], obj))
                sequence_length = len(sequence_items)
                normalized_items: list[Any] = []
                max_items = LLM_FRIENDLY_MAX_ARRAY_LENGTH

                for item in sequence_items[:max_items]:
                    normalized_items.append(
                        DataNormalizer.normalize(
                            item,
                            max_depth,
                            current_depth + 1,
                            visited,
                        ),
                    )

                if sequence_length > max_items:
                    normalized_items.append(f"... {sequence_length - max_items} more items")

                if isinstance(obj, tuple):
                    return tuple(normalized_items)
                if isinstance(obj, set):
                    return set(normalized_items)
                return normalized_items

            # Handle strings
            if isinstance(obj, str):
                max_length = LLM_FRIENDLY_MAX_STRING_LENGTH
                str_length = len(obj)
                if str_length > max_length:
                    return f"{obj[:max_length]}...{str_length - max_length} more characters"
                return obj

            # Handle dictionaries
            if isinstance(obj, dict):
                dict_result: dict[Any, Any] = {}
                dict_obj = cast(dict[Any, Any], obj)
                dict_keys_list: list[Any] = list(dict_obj.keys())
                max_props = LLM_FRIENDLY_MAX_DICT_KEYS

                for _, key in enumerate(dict_keys_list[:max_props]):
                    try:
                        dict_result[key] = DataNormalizer.normalize(
                            dict_obj[key],
                            max_depth,
                            current_depth + 1,
                            visited,
                        )
                    except Exception:
                        dict_result[key] = "[Error accessing property]"

                if len(dict_keys_list) > max_props:
                    dict_result["..."] = f"{len(dict_keys_list) - max_props} more properties"

                return dict_result

            # Fallback for other types
            return f"[{type(obj).__name__}]"

        finally:
            visited.discard(obj_id)

    @staticmethod
    def estimate_size(obj: Any) -> int:
        """
        Estimate the size of an object in bytes.

        Args:
            obj: Object to estimate size for

        Returns:
            Estimated size in bytes
        """
        try:
            # Fast estimation without full serialization
            sample = json.dumps(obj, default=str)[:1000]
            avg_char_size = len(sample.encode("utf-8")) / len(sample) if sample else 1

            full_length = DataNormalizer._estimate_json_length(obj)
            return int(full_length * avg_char_size)
        except Exception:
            return sys.getsizeof(obj)

    @staticmethod
    def _estimate_json_length(obj: Any, visited: set[int] | None = None) -> int:
        """
        Estimate the JSON string length of an object.

        Args:
            obj: Object to estimate length for
            visited: Set of visited object IDs

        Returns:
            Estimated JSON string length
        """
        if visited is None:
            visited = set()

        if obj is None:
            return 4  # "null"
        if isinstance(obj, bool):
            return 4 if obj else 5  # "true" : "false"
        if isinstance(obj, (int, float)):
            return len(str(obj))
        if isinstance(obj, str):
            return len(obj) + 2  # quotes

        obj_id = id(obj)
        if obj_id in visited:
            return 12  # "[Circular]"
        visited.add(obj_id)

        try:
            if isinstance(obj, (list, tuple, set)):
                iterable_obj = cast(Iterable[Any], obj)
                length = 2  # []
                for item in iterable_obj:
                    length += DataNormalizer._estimate_json_length(item, visited) + 1  # comma
                return length

            if isinstance(obj, dict):
                mapping_obj = cast(Mapping[Any, Any], obj)
                length = 2  # {}
                for key, value in mapping_obj.items():
                    length += len(str(key)) + 3  # "key":
                    length += (
                        DataNormalizer._estimate_json_length(
                            value,
                            visited,
                        )
                        + 1
                    )  # comma
                return length

            return 10  # Default estimate

        finally:
            visited.discard(obj_id)

    @staticmethod
    def _truncate_stack(stack: str, max_lines: int = 10) -> str:
        """
        Truncate stack trace to specified number of lines.

        Args:
            stack: Stack trace string
            max_lines: Maximum number of lines to keep

        Returns:
            Truncated stack trace
        """
        if not stack:
            return stack

        lines = stack.split("\n")
        if len(lines) <= max_lines:
            return stack

        return "\n".join(lines[:max_lines]) + f"\n... {len(lines) - max_lines} more lines"

    @staticmethod
    def _format_traceback(tb: TracebackType | None) -> str:
        """
        Format traceback object to string.

        Args:
            tb: Traceback object

        Returns:
            Formatted traceback string
        """
        import traceback

        if tb is None:
            return ""

        return "".join(traceback.format_tb(tb))


def read_file(
    path: Annotated[
        str | None,
        "The file path to read.",
    ],
):
    """
    Read a file and return its content with intelligent format detection and processing.
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")

    normalizer = DataNormalizer()
    formatter = DataFrameFormatter()
    handler = FileFormatHandler()

    try:
        # Read content based on file extension using handler
        if path.endswith(".json"):
            content = handler.read_json(path)
        elif path.endswith(".jsonl"):
            content = handler.read_jsonl(path)
        elif path.endswith(".csv"):
            content = handler.read_csv(path)
        elif path.endswith(".tsv"):
            content = handler.read_tsv(path)
        elif path.endswith((".xlsx", ".xls")):
            content = handler.read_excel(path)
        elif path.endswith(".parquet"):
            content = handler.read_parquet(path)
        elif path.endswith((".yaml", ".yml")):
            content = handler.read_yaml(path)
        elif path.endswith(".xml"):
            content = handler.read_xml(path)
        else:  # text files
            content = handler.read_text(path)

        # 如果读取的文件是纯文本，就额外多展示一些
        # 相反，如果是结构里面的字符串，截更短（因为结构里面有很多字符串）
        text_payload: str | None = None
        length = 0
        if isinstance(content, str):
            text_payload = content
            length = len(content)
        elif isinstance(content, ContentWithCustomLength) and isinstance(content.content, str):
            text_payload = content.content
            length = len(content)

        if text_payload is not None:
            max_length = LLM_FRIENDLY_MAX_STRING_LENGTH * LLM_FRIENDLY_MAX_ARRAY_LENGTH
            if length > max_length:
                truncated = text_payload[:max_length]
                return f"{truncated}...{length - max_length} more characters"
            return text_payload

        # Process content based on type
        if isinstance(content, pd.DataFrame):
            length = len(content)
            content = formatter.format_dataframe_with_types(
                content,
                sample_rows=min(LLM_FRIENDLY_MAX_SAMPLE_ROWS, length),
            )
            if length > LLM_FRIENDLY_MAX_SAMPLE_ROWS:
                content["note"] = f"This is a sample of the first {min(LLM_FRIENDLY_MAX_SAMPLE_ROWS, length)} rows. Total rows: {length}"
        return normalizer.normalize_to_size(
            content,
            max_depth=LLM_FRIENDLY_MAX_DEPTH,
            max_size_in_bytes=LLM_FRIENDLY_MAX_SIZE_IN_BYTES,
        )

    except Exception as e:
        return {
            "error": f"Failed to read file {path}: {str(e)}",
            "file_path": path,
            "file_extension": Path(path).suffix if os.path.exists(path) else "unknown",
        }


# Example usage
if __name__ == "__main__1":
    # Test with various data types

    def _double(value: int) -> int:
        return value * 2

    test_data: dict[str, Any] = {
        "string": "Hello World",
        "number": 42,
        "float": 3.14,
        "boolean": True,
        "none": None,
        "list": [1, 2, 3, {"nested": "value"}],
        "dict": {"key": "value", "nested": {"deep": "data"}},
        "datetime": datetime.now(),
        "function": _double,
        "many_dicts": [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7}] * 1000,
    }

    normalizer = DataNormalizer()
    normalized = normalizer.normalize_to_size(
        test_data,
        max_depth=5,
        max_size_in_bytes=1500,
    )

    print("Normalized data:")
    print(json.dumps(normalized, indent=2, default=str))

    print(f"\nEstimated size: {normalizer.estimate_size(normalized)} bytes")
