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

import inspect
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

from flask import current_app as app, request
from flask_caching import Cache
from flask_caching.backends import NullCache
from werkzeug.wrappers import Response

from superset import db
from superset.constants import CACHE_DISABLED_TIMEOUT
from superset.extensions import cache_manager
from superset.models.cache import CacheKey
from superset.utils.cache_manager import configurable_hash_method
from superset.utils.hashing import hash_from_dict
from superset.utils.json import json_int_dttm_ser

logger = logging.getLogger(__name__)


def generate_cache_key(values_dict: dict[str, Any], key_prefix: str = "") -> str:
    """Generate a deterministic cache key by hashing a dictionary."""
    hash_str = hash_from_dict(values_dict, default=json_int_dttm_ser)
    cache_key = f"{key_prefix}{hash_str}"

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Cache key generated: %s from dict keys: %s",
            cache_key,
            list(values_dict.keys()),
        )

    return cache_key


def set_and_log_cache(
    cache_instance: Cache,
    cache_key: str,
    cache_value: dict[str, Any],
    cache_timeout: int | None = None,
    datasource_uid: str | None = None,
) -> None:
    """Store a value in the cache and optionally persist the key to the metadata DB."""
    if isinstance(cache_instance.cache, NullCache):
        return

    timeout = (
        cache_timeout
        if cache_timeout is not None
        else app.config["CACHE_DEFAULT_TIMEOUT"]
    )

    if timeout == CACHE_DISABLED_TIMEOUT:
        return

    try:
        dttm = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        value = {**cache_value, "dttm": dttm}
        cache_instance.set(cache_key, value, timeout=timeout)
        app.config["STATS_LOGGER"].incr("set_cache_key")

        logger.debug(
            "CACHE SET - Key: %s, Datasource: %s, Timeout: %s",
            cache_key,
            datasource_uid,
            timeout,
        )

        if datasource_uid and app.config["STORE_CACHE_KEYS_IN_METADATA_DB"]:
            ck = CacheKey(
                cache_key=cache_key,
                cache_timeout=cache_timeout,
                datasource_uid=datasource_uid,
            )
            db.session.add(ck)
    except Exception as ex:  # pylint: disable=broad-except
        logger.warning("Could not cache key %s", cache_key)
        logger.exception(ex)


ONE_YEAR = 365 * 24 * 60 * 60  # seconds


def memoized_func(key: str, cache: Cache = cache_manager.cache) -> Callable[..., Any]:
    """
    Decorator with configurable key and cache backend.

    Usage::

        @memoized_func(key="{a}+{b}", cache=cache_manager.data_cache)
        def sum(a: int, b: int) -> int:
            return a + b

    The decorated function accepts the following extra keyword arguments at
    call time:

    - ``cache`` (bool): Enable/disable caching (default ``True``).
    - ``force`` (bool): Force a cache refresh (default ``False``).
    - ``cache_timeout`` (int): Override the default timeout in seconds.

    :param key: A format-string interpolated with the function's bound arguments.
    :param cache: A Flask-Caching instance to store results.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped_f(*args: Any, **kwargs: Any) -> Any:
            should_cache = kwargs.pop("cache", True)
            force = kwargs.pop("force", False)
            cache_timeout = kwargs.pop(
                "cache_timeout", app.config["CACHE_DEFAULT_TIMEOUT"]
            )

            if not should_cache:
                return f(*args, **kwargs)

            signature = inspect.signature(f)
            bound_args = signature.bind(*args, **kwargs)
            bound_args.apply_defaults()
            cache_key = key.format(**bound_args.arguments)

            obj = cache.get(cache_key)
            if not force and obj is not None:
                return obj
            obj = f(*args, **kwargs)

            if cache_timeout != CACHE_DISABLED_TIMEOUT:
                cache.set(cache_key, obj, timeout=cache_timeout)
            return obj

        return wrapped_f

    return wrap


def _is_cache_stale(
    response: Response | None,
    content_changed_time: datetime,
) -> bool:
    """Return True if the cached response is older than *content_changed_time*."""
    return bool(
        response
        and response.last_modified
        and response.last_modified.timestamp() < content_changed_time.timestamp()
    )


def _apply_cache_headers(
    response: Response,
    content_changed_time: datetime,
    timeout: int | float,
    *,
    must_revalidate: bool,
) -> None:
    """Set Last-Modified, Expires, ETag and Cache-Control on *response*."""
    if must_revalidate:
        response.cache_control.no_cache = True
    else:
        response.cache_control.public = True

    response.last_modified = content_changed_time
    expiration = timeout or ONE_YEAR
    response.expires = response.last_modified + timedelta(seconds=expiration)
    response.add_etag()


def _get_cached_response(
    cache: Cache,
    make_cache_key: Callable[..., str],
    f: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str | None, Response | None]:
    """Attempt to retrieve a cached response; return (cache_key, response)."""
    try:
        key_args = list(args)
        key_kwargs = kwargs.copy()
        key_kwargs.update(request.args)
        cache_key = make_cache_key(f, *key_args, **key_kwargs)
        return cache_key, cache.get(cache_key)
    except Exception:  # pylint: disable=broad-except
        if app.debug:
            raise
        logger.exception("Exception possibly due to cache backend.")
        return None, None


def _store_response(
    cache: Cache,
    cache_key: str | None,
    response: Response,
    timeout: int | float,
) -> None:
    """Persist *response* in the cache backend (best-effort)."""
    if cache_key is None:
        return
    try:
        cache.set(cache_key, response, timeout=timeout)
    except Exception:  # pylint: disable=broad-except
        if app.debug:
            raise
        logger.exception("Exception possibly due to cache backend.")


def etag_cache(
    cache: Cache = cache_manager.cache,
    get_last_modified: Callable[..., datetime] | None = None,
    max_age: int | float | None = None,
    raise_for_access: Callable[..., Any] | None = None,
    skip: Callable[..., bool] | None = None,
) -> Callable[..., Any]:
    """
    Decorator for caching views and handling ETag conditional requests.

    Adds Last-Modified, Expires and ETag headers to GET responses. Handles
    conditional requests via the If-Match / If-None-Match headers.  POST
    requests bypass the response cache but still benefit from the dataframe
    cache.
    """

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        timeout = max_age or app.config["CACHE_DEFAULT_TIMEOUT"]

        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Response:
            if raise_for_access:
                try:
                    raise_for_access(*args, **kwargs)
                except Exception:  # pylint: disable=broad-except
                    return f(*args, **kwargs)

            if request.method == "POST" or (skip and skip(*args, **kwargs)):
                return f(*args, **kwargs)

            cache_key, response = _get_cached_response(
                cache,
                wrapper.make_cache_key,  # type: ignore[attr-defined]
                f,
                args,
                kwargs,
            )

            content_changed_time = datetime.now(tz=timezone.utc)
            if get_last_modified:
                content_changed_time = get_last_modified(*args, **kwargs)
                if _is_cache_stale(response, content_changed_time):
                    response = None

            if response is None:
                response = f(*args, **kwargs)
                _apply_cache_headers(
                    response,
                    content_changed_time,
                    timeout,
                    must_revalidate=bool(get_last_modified or raise_for_access),
                )
                _store_response(cache, cache_key, response, timeout)

            return response.make_conditional(request)

        wrapper.uncached = f  # type: ignore
        wrapper.cache_timeout = timeout  # type: ignore
        wrapper.make_cache_key = cache._memoize_make_cache_key(  # type: ignore # pylint: disable=protected-access
            make_name=None, timeout=timeout, hash_method=configurable_hash_method
        )

        return wrapper

    return decorator
