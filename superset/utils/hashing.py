# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Literal

from flask import current_app

from superset.utils import json

HashAlgorithm = Literal["md5", "sha256"]

_HASH_FUNCTIONS: dict[HashAlgorithm, Callable[[bytes], str]] = {
    "sha256": lambda data: hashlib.sha256(data).hexdigest(),
    "md5": lambda data: hashlib.md5(data).hexdigest(),  # noqa: S324
}


def get_hash_algorithm() -> HashAlgorithm:
    """Return the configured hash algorithm for non-cryptographic purposes."""
    return current_app.config["HASH_ALGORITHM"]


def hash_from_str(val: str, algorithm: HashAlgorithm | None = None) -> str:
    """Generate a hex digest from ``val`` using the given algorithm."""
    if algorithm is None:
        algorithm = get_hash_algorithm()

    hash_func = _HASH_FUNCTIONS.get(algorithm)
    if hash_func is None:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")

    return hash_func(val.encode("utf-8"))


def hash_from_dict(
    obj: dict[Any, Any],
    ignore_nan: bool = False,
    default: Callable[[Any], Any] | None = None,
    algorithm: HashAlgorithm | None = None,
) -> str:
    """Generate a hex digest from a dictionary by JSON-serializing with sorted keys."""
    json_data = json.dumps(
        obj, sort_keys=True, ignore_nan=ignore_nan, default=default, allow_nan=True
    )
    return hash_from_str(json_data, algorithm=algorithm)
