# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""
Module for VTLookup class.

Wrapper class around `Virus Total
API <https://www.virustotal.com/en/documentation/public-api/>`__. Input
can be a single IoC observable or a pandas DataFrame containing multiple
observables. Processing requires a Virus Total account and API key and
processing performance is limited to the number of requests per minute
for the account type that you have. Support IoC Types:

-  Filehash
-  URL
-  DNS Domain
-  IPv4 Address

"""
from __future__ import annotations

import contextlib
import json
import logging
from json import JSONDecodeError
from typing import Any, ClassVar, Hashable, Mapping, NamedTuple

import httpx
import pandas as pd
from typing_extensions import Self

from ..._version import VERSION
from ...common.pkg_config import get_http_timeout
from ...common.utility import export, mp_ua_header
from ..lookup_result import SanitizedObservable
from ..preprocess_observable import preprocess_observable

logger: logging.Logger = logging.getLogger(__name__)
__version__ = VERSION
__author__ = "Ian Hellen"


class VTParams(NamedTuple):
    """VirusTotal parameter collection."""

    api_type: str
    batch_size: int
    batch_delimiter: str
    http_verb: str
    api_var_name: str
    headers: dict[str, Any] | None


class DuplicateStatus(NamedTuple):
    """Information about vt objects being duplicates."""

    is_dup: bool
    status: str


@export
class VTLookup:
    """
    VTLookup: VirusTotal lookup of IoC reports.

    Main methods are:
    lookup_iocs() - accepts input of multiple IoCs in a Pandas DataFrame
    lookup_ioc() - looks up a single IoC observable.
    supported_ioc_types - a list of valid target types.
    ioc_vt_type_mapping - a dictionary of mappings to recognized VT Types.
    Types mapped to None will not be submitted to VT.

    For urls a full http request can be submitted, query string and fragments will be
    dropped before submitting.
    For files MD5, SHA1 and SHA256 hashes are supported.
    For IP addresses only dotted IPv4 addresses are supported.
    """

    # Ioc types that we support
    _SUPPORTED_INPUT_TYPES: ClassVar[list[str]] = [
        "ipv4",
        "dns",
        "url",
        "md5_hash",
        "sha1_hash",
        "sh256_hash",
    ]

    # Mapping to to VT Types
    _VT_TYPE_MAP: ClassVar[dict[str, str]] = {
        "ipv4": "ip-address",
        "dns": "domain",
        "url": "url",
        "md5_hash": "file",
        "sha1_hash": "file",
        "sh256_hash": "file",
    }

    # VT API parameters
    _HDR_GZIP: ClassVar[dict[str, str]] = {"Accept-Encoding": "gzip, deflate"}
    _VT_API: ClassVar[str] = "https://www.virustotal.com/vtapi/v2/{type}/report"
    _VT_API_TYPES: ClassVar[dict[str, VTParams]] = {
        "url": VTParams("url", 1, "\n", "get", "resource", _HDR_GZIP),
        "file": VTParams("file", 25, ",", "get", "resource", _HDR_GZIP),
        "ip-address": VTParams("ip-address", 1, "", "get", "ip", None),
        "domain": VTParams("domain", 1, "", "get", "domain", None),
    }

    _RESULT_COLUMNS: ClassVar[list[str]] = [
        "Observable",
        "IoCType",
        "Status",
        "ResponseCode",
        "RawResponse",
        "Resource",
        "SourceIndex",
        "VerboseMsg",
        "Resource",
        "ScanId",
        "Permalink",
        "Positives",
        "MD5",
        "SHA1",
        "SHA256",
        "ResolvedDomains",
        "ResolvedIPs",
        "DetectedUrls",
    ]

    _http_strict_rgxc: None = None

    def __init__(self: VTLookup, vtkey: str, verbosity: int = 1) -> None:
        """
        Create a new instance of VTLookup class.

        Parameters
        ----------
        vtkey : str
            VirusTotal API key
        verbosity : int, optional
            The level of detail of reporting
                0 = no reporting
                1 = minimal reporting (default)
                2 = verbose reporting

        """
        self._vtkey: str = vtkey
        self._verbosity: int = verbosity
        self._ioc_custom_type_map: dict[str, str | None] = {}

        # create a data frame to store the results
        self.results: pd.DataFrame = pd.DataFrame(
            data=None,
            columns=self._RESULT_COLUMNS,
        )

    @property
    def supported_ioc_types(self: Self) -> list[str]:
        """
        Return list of supported IoC type internal names.

        Returns
        -------
        list[str]
            list of supported IoC type internal names.

        """
        return self._SUPPORTED_INPUT_TYPES

    @property
    def supported_vt_types(self: Self) -> list[str]:
        """
        Return list of VirusTotal supported IoC type names.

        Returns
        -------
        list[str]
            list of VirusTotal supported IoC type names.

        """
        return list(self._VT_API_TYPES.keys())

    @property
    def ioc_vt_type_mapping(self: Self) -> dict[str, str]:
        """
        Return mapping between internal and VirusTotal IoC type names.

        Returns
        -------
        Mapping[str, str]
            Return mapping between internal and VirusTotal IoC type names.

        """
        return self._VT_TYPE_MAP

    def lookup_iocs(
        self: Self,
        data: pd.DataFrame,
        src_col: str = "Observable",
        type_col: str = "IoCType",
        src_index_col: str = "SourceIndex",
        **kwargs: str,
    ) -> pd.DataFrame:
        """
        Retrieve results for IoC observables in the source dataframe.

        Parameters
        ----------
        data : pd.DataFrame
            Dataframe containing the observables to search for
        src_col : str, optional
            The column name that contains the observable data
            (one item per row) (the default is 'Observable')
        type_col : str, optional
            The column name containing the observable type
            (the default is 'IoCType')
        src_index_col : str, optional
            The name of the column to use as source index. If not
            specified this defaults to 'SourceIndex'. If this (or the supplied value)
            is not in the source dataframe, the index of the source dataframe will
            be used. This is retained in the output so that you can join the results
            back to the original data.
            (the default is 'SourceIndex')
        kwargs: str
            key/value pairs of additional mappings to supported IoC type names
            e.g. ipv4='ipaddress', url='httprequest'.
            This allows you to specify custom
            mappings when the source data is tagged with different names.

        Returns
        -------
        pd.DataFrame
            Combined results of local pre-processing and VirusTotal Lookups

        Raises
        ------
        KeyError
            Unknown ioc_type

        Notes
        -----
            See supported_ioc_types attribute for a list of valid target types.
            Not all of these types are supported by VirusTotal.
            See ioc_vt_type_mapping for current mappings.
            Types mapped to None will not be submitted to VT.

            For urls a full http request can be submitted, query string
            and fragments will be dropped before submitting.
            Other supported protocols are ftp, telnet, ldap, file
            For files MD5, SHA1 and SHA256 hashes are supported.
            For IP addresses only dotted IPv4 addresses are supported.

        """
        # if the caller has supplied alternative type name mappings add any of these
        # to our lookup dictionary
        for k in self._get_supported_vt_ioc_types():
            self._ioc_custom_type_map[k] = k
        for k, val in kwargs.items():
            if k in self._get_supported_vt_ioc_types():
                self._ioc_custom_type_map[k] = val

        src_idx_col: str | None = src_index_col if src_index_col in data else None

        # for each ioc_type, retrieve observables from dataframe
        for ioc_type, mapped_type in self._ioc_custom_type_map.items():
            input_df: pd.DataFrame = data[data[type_col] == mapped_type]
            self._lookup_ioc_type(input_df, ioc_type, src_col, src_idx_col)

        self._print_status(
            f"Submission complete. {len(self.results)} responses from {len(data)} input rows",
            2,
        )

        return self.results

    def lookup_ioc(
        self: Self,
        observable: str,
        ioc_type: str,
        output: str = "dict",
    ) -> pd.DataFrame | list[dict]:
        """
        Look up and single IoC observable.

        Parameters
        ----------
        observable : str
            The observable value
        ioc_type : str
            The IoC Type (see 'supported_ioc_types' attribute)
        output : str, optional
            Output results as a dictionary (or list of dicts)
            if `output` is any other value the result will be returned in a
            Pandas DataFrame (the default is 'dict')

        Returns
        -------
            list{dict}: if output == 'dict'
            pd.DataFrame: otherwise

        Raises
        ------
        KeyError
            Unknown ioc_type

        """
        # Check input
        if (
            observable is None
            or observable.strip() is None
            or ioc_type is None
            or ioc_type.strip() is None
        ):
            error_msg: str = "Invalid value for observable or ioc_type"
            raise SyntaxError(error_msg)

        pp_observable, status = preprocess_observable(observable, ioc_type)
        if pp_observable is None:
            error_msg = f"{status} for observable value {observable}"
            raise SyntaxError(error_msg)

        if ioc_type not in self._VT_TYPE_MAP:
            error_msg = (
                f"IoC Type {ioc_type} not recognized. "
                f"Valid types are [{', '.join(self.supported_ioc_types)}]"
            )
            raise LookupError(error_msg)

        if self._VT_TYPE_MAP[ioc_type] not in self._VT_API_TYPES:
            vt_types: set[str] = {
                k for k, val in self.ioc_vt_type_mapping.items() if val is not None
            }
            err: tuple[str, str] = (
                f"IoC Type {ioc_type} is recognized by VirusTotal.",
                f"Valid types are [{'', ''.join(vt_types)}]",
            )
            raise LookupError(err)

        # do the submission
        vt_api_type: str = self._VT_TYPE_MAP[ioc_type]
        vt_param: VTParams = self._VT_API_TYPES[vt_api_type]
        results, _ = self._vt_submit_request(pp_observable, vt_param)
        self._parse_vt_results(results, pp_observable, ioc_type)

        # return as a list of dictionaries or a DataFrame
        if output == "dict":
            list_res: list = self.results.apply(
                lambda x: x.to_dict(),
                axis=1,
            ).tolist()
            return list_res[0] if len(list_res) == 1 else list_res

        return self.results

    # pylint: disable=too-many-locals
    def _lookup_ioc_type(
        self: Self,
        input_frame: pd.DataFrame,
        ioc_type: str,
        src_col: str,
        src_index_col: str | None,
    ) -> None:
        """
        Perform the VT submission of a set of IoCs of a given type.

        Parameters
        ----------
        input_frame : pd.DataFrame
            the input dataframe
        ioc_type : str
            the IoC Type to submit
        src_col : str
            The name column in the dataframe containing the
            IoC observables
        src_index_col : Optional[str]
            SourceIndex column name

        Raises
        ------
        KeyError
            Unknown ioc_type

        """
        if ioc_type not in self._VT_TYPE_MAP:
            error_msg: str = f'Unknown ioc_type "{ioc_type}""'
            raise KeyError(error_msg)

        vt_param: VTParams = self._VT_API_TYPES[self._VT_TYPE_MAP[ioc_type]]

        # Some types support batch lookups so we can assemble them into batches
        # for the moment we are only supporting
        source_row_index: dict[str, Any] = {}
        obs_batch: list[str] = []
        batch_index: int = 0
        row_count: int = len(input_frame)
        if src_index_col:
            src_cols: list[str] = [src_col, src_index_col]
        else:
            src_cols = [src_col]

        for row_num, (idx, row) in enumerate(input_frame[src_cols].iterrows(), start=1):
            observable: str = row[src_col]

            index: Hashable = idx
            # Use the user-specified index if possible
            if src_index_col:
                index = row[src_index_col]

            # validate the observable to avoid sending too much junk to VT
            pp_observable: SanitizedObservable = self._validate_observable(
                observable,
                ioc_type,
                index,
            )

            # if the observable is valid, add it to the submission batch
            if pp_observable.observable:
                obs_batch.append(pp_observable.observable)
                source_row_index[pp_observable.observable] = index
                batch_index += 1

            # We want to trigger in the following circumstances
            # 1. if the length of our batch is at the max VT batchsize for
            # this type (If the batch size is 1 this will fire for every row)
            # 2. Or we have reached the end of our row iteration
            # AND
            # 3. The batch is not empty
            if (
                len(obs_batch) == vt_param.batch_size or row_num == row_count
            ) and obs_batch:
                obs_submit: str = vt_param.batch_delimiter.join(obs_batch)

                self._print_status(
                    (
                        "Submitting observables: "
                        f'"{obs_submit}", type "{ioc_type}" '
                        f"to VT. (Source index {index})"
                    ),
                    2,
                )
                # Submit the request
                results, status_code = self._vt_submit_request(obs_submit, vt_param)

                if status_code != httpx.codes.OK:
                    # Print status messages and add failure cases to results
                    status: str = f"Failed submission: http error {status_code}"
                    for failed_obs in obs_batch:
                        self._add_invalid_input_result(
                            failed_obs,
                            ioc_type,
                            status,
                            source_row_index[failed_obs],
                        )
                        self._print_status(
                            "Error in response submitting observables: "
                            f"'{obs_submit}', type '{ioc_type}'"
                            f"http status is {status_code}. "
                            f"Response: {results} "
                            f"(Source index {source_row_index[failed_obs]}",
                            1,
                        )
                else:
                    # parse the results from the response
                    self._parse_vt_results(
                        results,
                        obs_submit,
                        ioc_type,
                        index,
                        source_row_index,
                        vt_param,
                    )

                # reset index of batch
                batch_index = 0
                obs_batch = []

    def _parse_vt_results(  # noqa:PLR0913
        self: Self,
        vt_results: str | list | dict | None,
        observable: str,
        ioc_type: str,
        source_idx: Hashable = 0,
        source_row_index: dict[str, Any] | None = None,
        vt_param: VTParams | None = None,
    ) -> None:
        """
        Parse VirusTotal results based on IoCType.

            :param vt_results: Raw results from VT
            :param observable: The observable or observable batch
            :param ioc_type: The IoC type of the observables
            :param source_idx: The row index of the source frame
            :param source_row_index: (batch only) Mapping between observable item
                and row index of the source
            :param vt_param: (batch only) the VTParams tuple for this submission

        """
        results_to_parse: list[dict] = []
        if isinstance(vt_results, str):
            with contextlib.suppress(JSONDecodeError, TypeError):
                vt_results = json.loads(vt_results, strict=False)

        if (
            isinstance(vt_results, list)
            and vt_param is not None
            and vt_param.batch_size > 1
        ):
            # multiple results
            results_to_parse = vt_results
        elif isinstance(vt_results, dict):
            # single result
            results_to_parse.append(vt_results)
        else:
            self._print_status(
                (
                    "Error parsing response to JSON: "
                    f'"{observable}", type "{ioc_type}". '
                    f"(Source index {source_idx})"
                ),
                1,
            )

        if vt_param and vt_param.batch_delimiter:
            observables: list[str] = observable.split(vt_param.batch_delimiter)
        else:
            observables = [observable]

        for result_idx, _ in enumerate(results_to_parse):
            df_dict_vtresults: pd.DataFrame = self._parse_single_result(
                results_to_parse[result_idx],
                ioc_type,
            )

            # Add remaining fields from source
            df_dict_vtresults["IoCType"] = ioc_type
            df_dict_vtresults["Status"] = "Success"
            df_dict_vtresults["RawResponse"] = json.dumps(results_to_parse[result_idx])
            if (
                len(results_to_parse) == 1
                or source_row_index is None
                or len(source_row_index) == 1
            ):
                df_dict_vtresults["Observable"] = observable
                df_dict_vtresults["SourceIndex"] = source_idx
            elif "resource" in results_to_parse[result_idx]:
                # If we submitted multiple values in a batch
                # we assume (hope) that the ordering of the response is the same
                # as in the request. We try our best to re-marry the observable
                # and source index
                vt_resource = results_to_parse[result_idx]["resource"]
                df_dict_vtresults["Observable"] = vt_resource
                if vt_resource in source_row_index:
                    df_dict_vtresults["SourceIndex"] = source_row_index[vt_resource]
                else:
                    df_dict_vtresults["SourceIndex"] = source_row_index[
                        observables[result_idx]
                    ]
            else:
                df_dict_vtresults["Observable"] = observables[result_idx]
                df_dict_vtresults["SourceIndex"] = source_row_index[
                    observables[result_idx]
                ]

            new_results: pd.DataFrame = pd.concat(
                objs=[self.results, df_dict_vtresults],
                ignore_index=True,
                axis=0,
            )

            self.results = new_results

    def _parse_single_result(
        self: Self,
        results_dict: Mapping[str, Any],
        ioc_type: str,
    ) -> pd.DataFrame:
        """
        Parse VirusTotal single result based on IoCType.

        Parameters
        ----------
        results_dict : Mapping[str, Any]
            Raw results dictionary from VT
        ioc_type : str
            The IoC type of the observables

        Returns
        -------
        pd.DataFrame
            The results DataFrame

        """
        # create output frame and parse results to intermediate frame
        df_dict_vtresults: dict[str, Any] = {}

        # Parse returned results to our output dataframe depending
        # on the IoC type
        if ioc_type in ["url", "md5_hash", "sha1_hash", "sha256_hash"]:
            df_dict_vtresults["ResponseCode"] = results_dict.get("response_code", None)
            df_dict_vtresults["VerboseMsg"] = results_dict.get("verbose_msg", None)
            df_dict_vtresults["ScanId"] = results_dict.get("scan_id", None)
            df_dict_vtresults["Resource"] = results_dict.get("resource", None)
            df_dict_vtresults["Permalink"] = results_dict.get("permalink", None)
            df_dict_vtresults["Positives"] = results_dict.get("positives", None)
            if ioc_type in ["md5_hash", "sha1_hash", "sha256_hash"]:
                df_dict_vtresults["MD5"] = results_dict.get("md5", None)
                df_dict_vtresults["SHA1"] = results_dict.get("sha1", None)
                df_dict_vtresults["SHA256"] = results_dict.get("sha256", None)

        if ioc_type in ["ipv4", "dns"]:
            df_dict_vtresults["ResponseCode"] = results_dict.get("response_code", None)
            df_dict_vtresults["VerboseMsg"] = results_dict.get("verbose_msg", None)
            # dns and ipv4 have multi-valued 'resolutions' and 'detected_urls' lists
            # of dictionaries
            # This leads to a few horrendous-looking list comprehensions
            # These are essentially pulling out the columns that contain these lists.
            # then using a list comprehension to pull out the value, where the key 'k'
            # is of the required value
            if ioc_type == "ipv4" and "resolutions" in results_dict:
                item_list: list = [
                    item["hostname"]
                    for item in results_dict["resolutions"]
                    if "hostname" in item
                ]
                df_dict_vtresults["ResolvedDomains"] = ", ".join(item_list)
            elif ioc_type == "dns" and "resolutions" in results_dict:
                item_list = [
                    item["ip_address"]
                    for item in results_dict["resolutions"]
                    if "ip_address" in item
                ]
                df_dict_vtresults["ResolvedIPs"] = ", ".join(item_list)
            if "detected_urls" in results_dict:
                item_list = [
                    item["url"]
                    for item in results_dict["detected_urls"]
                    if "url" in item
                ]
                df_dict_vtresults["DetectedUrls"] = ", ".join(item_list)
                # positives are listed per detected_url so we need to
                # pull those our and sum them.
                positives = sum(
                    item["positives"]
                    for item in results_dict["detected_urls"]
                    if "positives" in item
                )
                df_dict_vtresults["Positives"] = positives

        return pd.DataFrame(
            data=df_dict_vtresults,
            columns=self._RESULT_COLUMNS,
            index=[0],
        )

    def _validate_observable(
        self: Self,
        observable: str,
        ioc_type: str,
        idx: Hashable,
    ) -> SanitizedObservable:
        """
        Validate observable for format and duplicates of existing results.

        Parameters
        ----------
        observable : str
            The observable to be checked
        ioc_type : str
            The IoCType of the observable
        idx : Any
            The index of the source row

        Returns
        -------
        SanitizedObservable
            The Pre-processed result

        """
        if observable is None or observable.strip() is None:
            status = "Failed: Empty or missing observable value"
            self._add_invalid_input_result(observable, ioc_type, status, idx)
            self._print_status(f"{status} (Source index {idx})", 1)
            return SanitizedObservable(None, status)

        # Check that observable is of the correct format for this type
        # and do any cleaning up required
        pp_observable: SanitizedObservable = preprocess_observable(observable, ioc_type)
        if pp_observable.observable is None:
            self._add_invalid_input_result(
                observable,
                ioc_type,
                pp_observable.status or "Unknown status from preprocess_observable",
                idx,
            )
            self._print_status(
                (
                    f'Invalid observable format: "{observable}", '
                    f'type "{ioc_type}", '
                    f"status: {pp_observable.status} "
                    f"- skipping. (Source index {idx})"
                ),
                2,
            )
            return pp_observable

        # Check that we don't already have a result for this
        dup_result: DuplicateStatus = self._check_duplicate_submission(
            observable,
            ioc_type,
            idx,
        )
        if dup_result.is_dup:
            self._print_status(
                (
                    "Duplicate observable value detected: "
                    f'"{observable}", type "{ioc_type}" '
                    f"status: {dup_result.status} "
                    f"- skipping. (Source index {idx})"
                ),
                2,
            )
            return SanitizedObservable(None, dup_result.status)

        return pp_observable

    def _check_duplicate_submission(
        self: Self,
        observable: str,
        ioc_type: str,
        source_index: Hashable,
    ) -> DuplicateStatus:
        """
        Check for a duplicate value in existing results.

        Parameters
        ----------
        observable : str
             The IoC observable value
        ioc_type : str
            The IoC type
        source_index : Any
            The index of the source DataFrame row

        Returns
        -------
        DuplicateStatus
            Status indicating whether this is a duplicate.

        """
        if self.results is None:
            return DuplicateStatus(is_dup=False, status="ok")

        # Note duplicate var here can be multiple rows of past results
        duplicate: pd.DataFrame = self.results[
            self.results["Observable"] == observable
        ].copy()
        # if this is a file hash we should check for previous results in
        # all of the hash columns
        if duplicate.shape[0] == 0 and ioc_type in [
            "md5_hash",
            "sha1_hash",
            "sh256_hash",
        ]:
            dup_query = (
                "MD5 == @observable or SHA1 == @observable or SHA256 == @observable"
            )
            duplicate = self.results.query(dup_query).copy()
            # In these cases we want to set the observable to the source value
            # but keep the rest of the results
            if duplicate.shape[0] > 0:
                duplicate["Observable"] = observable

        # if we found a duplicate so add the copies of the duplicated requests
        # to the results
        if duplicate.shape[0] > 0:
            original_indices: list = [
                v[0] for v in duplicate[["SourceIndex"]].to_numpy()
            ]
            duplicate["SourceIndex"] = source_index
            duplicate["Status"] = "Duplicate"
            new_results: pd.DataFrame = pd.concat(
                objs=[self.results, duplicate],
                ignore_index=True,
                sort=False,
                axis=0,
            )
            self.results = new_results

            return DuplicateStatus(
                is_dup=True,
                status=f"Duplicates of {original_indices}",
            )

        return DuplicateStatus(is_dup=False, status="ok")

    def _add_invalid_input_result(
        self: Self,
        observable: str,
        ioc_type: str,
        status: str,
        source_idx: Hashable,
    ) -> None:
        """
        Add a result row to indicate an invalid submission.

        Parameters
        ----------
        observable : str
            The IoC observable value
        ioc_type : str
            The IoC type
        status : str
            The status - why the item was invalid
        source_idx : Any
            The index of the source DataFrame row

        """
        new_row = pd.Series(index=self._RESULT_COLUMNS)
        new_row["Observable"] = observable
        new_row["IoCType"] = ioc_type
        new_row["Status"] = status
        new_row["SourceIndex"] = source_idx
        new_results: pd.DataFrame = self.results.append(
            new_row.to_dict(),
            ignore_index=True,
        )

        self.results = new_results

    def _vt_submit_request(
        self: Self,
        submission_string: str,
        vt_param: VTParams,
    ) -> tuple[dict[Any, Any] | None, int]:
        """
        Submit the request to VT.

        Parameters
        ----------
        submission_string : str
            The observable (or observable collection)
        vt_param : VTParams
            VT parameters appropriate to this observable type

        """
        params: dict[str, str] = {
            "apikey": self._vtkey,
            vt_param.api_var_name: submission_string,
        }
        submit_url: str = self._get_vt_api_url(vt_param.api_type)
        headers: dict[str, str] = {
            **(mp_ua_header()),
            "Content-Type": "application/json",
        }
        if vt_param.headers is not None:
            headers.update(vt_param.headers)

        if vt_param.http_verb == "post":
            response: httpx.Response = httpx.post(
                submit_url,
                data=params,
                headers=headers,
                timeout=get_http_timeout(),
            )
        else:
            response = httpx.get(
                submit_url,
                params=params,
                headers=headers,
                timeout=get_http_timeout(),
            )
        if response.is_success:
            return response.json(), response.status_code

        if response:
            try:
                return response.json(), response.status_code
            except JSONDecodeError:
                pass
        return None, response.status_code

    @classmethod
    def _get_vt_api_url(cls: type[Self], api_type: str) -> str:
        """
        Return the VirusTotal API URL for the supplied type.

            :param api_type: The IoC type
        """
        if api_type not in cls._VT_API_TYPES:
            error_msg: str = f"Unknown api type '{api_type}'"
            raise LookupError(error_msg)
        return cls._VT_API.format(type=api_type)

    @classmethod
    def _get_supported_vt_ioc_types(cls: type[VTLookup]) -> list[str]:
        """Return the subset of IoC types supported by VT."""
        return [
            t for t in cls._SUPPORTED_INPUT_TYPES if cls._VT_TYPE_MAP[t] is not None
        ]

    def _print_status(self: Self, message: str, verbosity_level: int) -> None:
        """
        Print a status message depending on the current level of verbosity.

        Parameters
        ----------
        message : str
            the string message to print
        verbosity_level : int
            verbosity_level at which level the message should be output

        """
        if verbosity_level <= self._verbosity:
            logger.info(message)
