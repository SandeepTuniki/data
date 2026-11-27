# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
sdmx_cli.py

Command-line tool for downloading SDMX data and metadata.
Provides a simple interface to fetch data and metadata from SDMX REST APIs.
"""

import os
from typing import Any, Dict, List, NamedTuple, Optional
import urllib.parse

from absl import app
from absl import flags
from absl import logging
from sdmx_client import SdmxClient

# Command registry - single source of truth for commands and descriptions
_COMMAND_HANDLERS = {
    'download-metadata': {
        'handler': lambda args: handle_download_metadata(args),
        'description': 'Download SDMX metadata (XML format)'
    },
    'download-data': {
        'handler': lambda args: handle_download_data(args),
        'description': 'Download SDMX data with optional filters (CSV format)'
    }
}

# Flag definitions
FLAGS = flags.FLAGS


def _define_flags() -> None:
    """Defines command-line flags for this script."""
    # Common flags for both commands
    flags.DEFINE_string('endpoint', None, 'SDMX REST API endpoint URL')
    flags.DEFINE_string('agency', None, 'Agency ID (e.g., OECD.SDD.NAD)')
    flags.DEFINE_string('dataflow', None, 'Dataflow ID')
    flags.DEFINE_string('output_path', None, 'Output file path')

    # Data download specific flags - using multi_string for key:value pairs
    flags.DEFINE_multi_string(
        'key', [],
        'Data filters as key:value pairs (e.g., --key=FREQ:Q --key=REF_AREA:USA)'
    )
    flags.DEFINE_multi_string(
        'param', [],
        'Query parameters as key:value pairs (e.g., --param=startPeriod:2022)')

    # Logging flags
    flags.DEFINE_bool('verbose', False, 'Enable verbose logging')
    flags.DEFINE_bool('quiet', False, 'Only show errors')


class DownloadArgs(NamedTuple):
    """
    Values of the download flags, passed to the required-flags check and the
    command handlers.
    """
    endpoint: Optional[str]
    agency: Optional[str]
    dataflow: Optional[str]
    output_path: Optional[str]
    key: List[str]
    param: List[str]


def parse_key_value_pairs(pairs: List[str]) -> Dict[str, Any]:
    """
    Parse key:value pairs into a dictionary.

    Args:
        pairs: List of strings in format 'key:value'

    Returns:
        Dictionary with parsed key-value pairs

    Raises:
        ValueError: If any pair has invalid format
    """
    if not pairs:
        return {}

    result = {}
    for pair in pairs:
        if ':' not in pair:
            raise ValueError(f"Invalid format: {pair}. Expected key:value")

        key, value = pair.split(':', 1)
        result[key.strip()] = value.strip()

    return result


def validate_url(url: str) -> bool:
    """
    Validate URL format.

    Args:
        url: URL to validate

    Returns:
        True if valid, False otherwise

    Raises:
        ValueError: If URL parsing fails
    """
    try:
        result = urllib.parse.urlparse(url)
        return bool(result.scheme and result.netloc)
    except Exception as e:
        raise ValueError(f"URL parsing failed for '{url}'") from e


def ensure_output_directory(file_path: str) -> None:
    """
    Create output directory if it doesn't exist.

    Args:
        file_path: Output file path

    Raises:
        OSError: If directory creation fails
    """
    directory = os.path.dirname(file_path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
        logging.info(f"Created output directory: {directory}")


def handle_download_metadata(args: DownloadArgs) -> None:
    """
    Handle the download-metadata subcommand.

    Args:
        args: Values of the download flags

    Raises:
        ValueError: If endpoint URL is invalid
        Exception: If download fails
    """
    logging.info("Starting metadata download...")
    logging.info(f"Endpoint: {args.endpoint}")
    logging.info(f"Agency: {args.agency}")
    logging.info(f"Dataflow: {args.dataflow}")
    logging.info(f"Output: {args.output_path}")

    # Validate inputs
    if not validate_url(args.endpoint):
        raise ValueError(f"Invalid endpoint URL: {args.endpoint}")

    # Ensure output directory exists
    ensure_output_directory(args.output_path)

    # Create client and download metadata
    client = SdmxClient(args.endpoint, args.agency)
    client.download_metadata(args.dataflow, args.output_path)
    logging.info(f"Successfully downloaded metadata to: {args.output_path}")


def handle_download_data(args: DownloadArgs) -> None:
    """
    Handle the download-data subcommand.

    Args:
        args: Values of the download flags

    Raises:
        ValueError: If endpoint URL is invalid or key:value parsing fails
        Exception: If download fails
    """
    logging.info("Starting data download...")
    logging.info(f"Endpoint: {args.endpoint}")
    logging.info(f"Agency: {args.agency}")
    logging.info(f"Dataflow: {args.dataflow}")
    logging.info(f"Output: {args.output_path}")

    # Parse key and params from multi-value flags
    data_key = parse_key_value_pairs(args.key)
    data_params = parse_key_value_pairs(args.param)

    logging.info(f"Key filters: {data_key}")
    logging.info(f"Parameters: {data_params}")

    # Validate inputs
    if not validate_url(args.endpoint):
        raise ValueError(f"Invalid endpoint URL: {args.endpoint}")

    # Ensure output directory exists
    ensure_output_directory(args.output_path)

    # Create client and download data
    client = SdmxClient(args.endpoint, args.agency)
    client.download_data_as_csv(args.dataflow, data_key, data_params,
                                args.output_path)
    logging.info(f"Successfully downloaded data to: {args.output_path}")


def validate_required_flags_for_command(command: str,
                                        args: DownloadArgs) -> None:
    """
    Validate that required flags are provided for the given command.

    Args:
        command: The command being executed
        args: Values of the download flags

    Raises:
        ValueError: If required flags are missing
    """
    required_common = ['endpoint', 'agency', 'dataflow', 'output_path']
    missing_flags = []

    for flag_name in required_common:
        flag_value = getattr(args, flag_name)
        if not flag_value:
            missing_flags.append(f"--{flag_name}")

    if missing_flags:
        raise ValueError(
            f"Missing required flags for {command}: {', '.join(missing_flags)}")


def setup_logging(verbose: bool, quiet: bool) -> None:
    """
    Set the logging verbosity and stderr threshold from verbose and quiet.

    Args:
        verbose: If True, log debug messages (DEBUG instead of INFO)
        quiet: If True, log only errors (ERROR); takes precedence over verbose
    """
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.set_verbosity(level)
    logging.set_stderrthreshold(level)


def generate_usage_message() -> str:
    """
    Generate usage message dynamically from command handlers.

    Returns:
        Formatted usage string
    """
    msg = "Usage: sdmx_cli.py <command> [flags]\n\n"
    msg += "Commands:\n"
    for cmd, info in _COMMAND_HANDLERS.items():
        msg += f"  {cmd:<20} {info['description']}\n"
    msg += "\nUse --help for flag details\n"
    return msg


def main(argv) -> None:
    """Main entry point for the CLI tool."""
    # Check if command was provided
    if len(argv) < 2:
        print(generate_usage_message())
        return

    command = argv[1]

    # Validate command
    if command not in _COMMAND_HANDLERS:
        print(f"Unknown command '{command}'. "
              f"Valid commands: {', '.join(_COMMAND_HANDLERS.keys())}")
        return

    # Setup logging
    setup_logging(verbose=FLAGS.verbose, quiet=FLAGS.quiet)

    args = DownloadArgs(endpoint=FLAGS.endpoint,
                        agency=FLAGS.agency,
                        dataflow=FLAGS.dataflow,
                        output_path=FLAGS.output_path,
                        key=FLAGS.key,
                        param=FLAGS.param)

    # Validate required flags for the command
    validate_required_flags_for_command(command, args)

    # Route to appropriate handler using the mapping
    _COMMAND_HANDLERS[command]['handler'](args)


if __name__ == "__main__":
    _define_flags()
    app.run(main)
