#!/usr/bin/env python3
"""Measure fixed-volume RDMA perftest runs across configured links."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Literal


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = SCRIPT_DIR.parent / "transmission-results"
DECIMAL_BYTES_PER_GB = 1_000_000_000
BITS_PER_BYTE = 8
BITS_PER_GIGABIT = 1_000_000_000
MINIMUM_ITERATIONS = 100
MAXIMUM_ITERATIONS = 2_147_483_647
MAXIMUM_MESSAGE_BYTES = 2_147_483_647
DEFAULT_BASE_PORT = 18515
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_INITIAL_DELAY_SECONDS = 3
RUN_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*/run-[0-9]+$")
CSV_FIELDS = (
    "run_name", "mode", "verb", "device", "local_ip", "peer_ip",
    "transfer_bytes", "message_bytes", "iterations",
    "perftest_reported_average_Gbit_s", "estimated_data_phase_elapsed_s",
    "estimated_data_phase_rounding_bound_s", "data_phase_GBps",
    "client_process_wall_elapsed_s", "status",
)
PERFTEST_BANDWIDTH_HEADER_PATTERN = re.compile(
    r"BW average\s*\[\s*Gb(?:it)?/(?:sec|s)\s*\]")
PERFTEST_PEAK_BANDWIDTH_HEADER_PATTERN = re.compile(
    r"BW peak\s*\[\s*Gb(?:it)?/(?:sec|s)\s*\]")
PERFTEST_NUMBER_PATTERN = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
VALID_MODES = ("isolation", "concurrent")
VALID_VERBS = ("send", "write", "read")


class TransferArguments(argparse.Namespace):
    role: Literal["host", "client"]
    run_name: str
    config: Path
    peer: str | None
    modes: tuple[str, ...]
    verb: Literal["send", "write", "read"]
    transfer_bytes: int
    message_bytes: int
    base_port: int
    timeout: int
    initial_delay: int
    results: Path
    iterations: int


@dataclass(frozen=True)
class Link:
    """One configured RDMA device and its matching local/peer addresses."""

    device: str
    netdev: str
    local_ip: str
    peer_ip: str
    port_index: int


@dataclass
class RunningTest:
    """A launched perftest process and the log file receiving its output."""

    mode: str
    link: Link
    log_path: Path
    process: subprocess.Popen[bytes]
    log_file: BinaryIO
    started_ns: int


def positive_integer(value: str) -> int:
    """Parse a positive base-10 integer for command-line arguments."""
    try:
        parsed_value = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a base-10 integer") from error
    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed_value


def non_negative_integer(value: str) -> int:
    """Parse a non-negative base-10 integer for startup delay."""
    try:
        parsed_value = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a base-10 integer") from error
    if parsed_value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed_value


def mode_list(value: str) -> tuple[str, ...]:
    """Parse a unique comma-separated list of supported test modes."""
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError("must select at least one mode")
    if len(set(parts)) != len(parts):
        raise argparse.ArgumentTypeError("must not repeat a mode")
    invalid_modes = sorted(set(parts) - set(VALID_MODES))
    if invalid_modes:
        raise argparse.ArgumentTypeError(
            f"unsupported mode(s): {', '.join(invalid_modes)}")
    return parts


def run_name(value: str) -> str:
    """Validate a campaign/run path without allowing traversal segments."""
    if RUN_NAME_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "expected <campaign>/run-NNN using letters, digits, '.', '_', or '-'")
    return value


def parse_arguments() -> TransferArguments:
    """Parse suite options and calculate the exact perftest iteration count."""
    parser = argparse.ArgumentParser(
        description=("Estimate fixed-volume RDMA data-phase time from "
                     "perftest bandwidth reports on configured links."))
    parser.add_argument("role", choices=("host", "client"))
    parser.add_argument("--run-name", required=True, type=run_name,
                        help="shared campaign/run-NNN name for both systems")
    parser.add_argument("--config", required=True, type=Path,
                        help="this host's JSON config used by perftest-suite.sh")
    parser.add_argument("--peer",
                        help="peer name when the config defines multiple peers")
    parser.add_argument("--modes", type=mode_list,
                        default=mode_list("isolation,concurrent"),
                        help="comma-separated subset of isolation,concurrent")
    parser.add_argument("--verb", choices=VALID_VERBS, default="write")
    parser.add_argument("--transfer-bytes", type=positive_integer,
                        default=DECIMAL_BYTES_PER_GB,
                        help="payload bytes per link and mode; default: 1 GB")
    parser.add_argument("--message-bytes", type=positive_integer,
                        default=1_000_000,
                        help="bytes per RDMA operation; default: 1 MB")
    parser.add_argument("--base-port", type=positive_integer,
                        default=DEFAULT_BASE_PORT,
                        help="first perftest TCP port; default: 18515")
    parser.add_argument("--timeout", type=positive_integer,
                        default=DEFAULT_TIMEOUT_SECONDS,
                        help="maximum process time in seconds")
    parser.add_argument("--initial-delay", type=non_negative_integer,
                        default=DEFAULT_INITIAL_DELAY_SECONDS,
                        help="client delay before connecting to host servers")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_ROOT,
                        help="result root; default: transmission-results/")
    arguments = parser.parse_args(namespace=TransferArguments())
    if arguments.base_port > 65535:
        parser.error("--base-port must be in the range 1..65535")
    if arguments.transfer_bytes % arguments.message_bytes != 0:
        parser.error("--transfer-bytes must be divisible by --message-bytes")
    if arguments.message_bytes > MAXIMUM_MESSAGE_BYTES:
        parser.error(f"--message-bytes exceeds {MAXIMUM_MESSAGE_BYTES}")
    arguments.iterations = arguments.transfer_bytes // arguments.message_bytes
    if arguments.iterations < MINIMUM_ITERATIONS:
        parser.error(
            f"the suite requires at least {MINIMUM_ITERATIONS} operations per run")
    if arguments.iterations > MAXIMUM_ITERATIONS:
        parser.error(f"iteration count exceeds {MAXIMUM_ITERATIONS}")
    return arguments


def load_peer_mapping(config_path: Path, peer_name: str | None
                      ) -> tuple[str, str, dict[str, str]]:
    """Load the selected device-to-peer-IP mapping from a suite config."""
    try:
        config_value = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read config {config_path}: {error}") from error
    if not isinstance(config_value, dict) or config_value.get("schema_version") != 1:
        raise ValueError(f"unsupported config schema: {config_path}")
    node_name = config_value.get("node")
    peer_profiles = config_value.get("peers")
    if not isinstance(node_name, str) or not node_name:
        raise ValueError("config must contain a non-empty node name")
    if not isinstance(peer_profiles, dict) or not peer_profiles:
        raise ValueError("config must contain at least one peer profile")
    if peer_name is None:
        if len(peer_profiles) != 1:
            raise ValueError("--peer is required when config defines multiple peers")
        peer_name = next(iter(peer_profiles))
    peer_mapping = peer_profiles.get(peer_name)
    if not isinstance(peer_mapping, dict) or not peer_mapping:
        raise ValueError(f"config has no peer profile named {peer_name!r}")

    normalized_mapping: dict[str, str] = {}
    for device_name, peer_address in peer_mapping.items():
        if not isinstance(device_name, str) or not isinstance(peer_address, str):
            raise ValueError("peer mappings must contain device-name/IP strings")
        try:
            normalized_mapping[device_name] = str(
                ipaddress.IPv4Address(peer_address))
        except ipaddress.AddressValueError as error:
            raise ValueError(
                f"invalid peer IPv4 address for {device_name}: {peer_address}") from error
    return node_name, peer_name, normalized_mapping


def discover_device_addresses() -> dict[str, tuple[str, list[tuple[str, int]]]]:
    """Discover UP RDMA devices and their local IPv4 addresses."""
    sysfs_devices = Path("/sys/class/infiniband")
    if not sysfs_devices.is_dir():
        raise ValueError(f"RDMA sysfs directory not found: {sysfs_devices}")
    discovered: dict[str, tuple[str, list[tuple[str, int]]]] = {}
    for device_path in sorted(sysfs_devices.iterdir()):
        netdev_directory = device_path / "device" / "net"
        netdev_paths = sorted(netdev_directory.iterdir()) if netdev_directory.is_dir() else []
        if not netdev_paths:
            continue
        netdev = netdev_paths[0].name
        try:
            address_output = subprocess.run(
                ["ip", "-j", "addr", "show", "dev", netdev],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            address_data = json.loads(address_output)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read IPv4 addresses for {netdev}: {error}") from error
        if not address_data or address_data[0].get("operstate") != "UP":
            continue
        local_addresses: list[tuple[str, int]] = []
        for address_entry in address_data[0].get("addr_info", []):
            if address_entry.get("family") != "inet":
                continue
            local_address = address_entry.get("local")
            prefix_length = address_entry.get("prefixlen")
            if not isinstance(local_address, str) or not isinstance(prefix_length, int):
                continue
            try:
                normalized_address = str(ipaddress.IPv4Address(local_address))
            except ipaddress.AddressValueError:
                continue
            local_addresses.append((normalized_address, prefix_length))
        if local_addresses:
            discovered[device_path.name] = (netdev, local_addresses)
    return discovered


def canonical_link_key(local_ip: str, peer_ip: str) -> tuple[int, int]:
    """Return an endpoint-order-independent pair of IPv4 integers."""
    local_number = int(ipaddress.IPv4Address(local_ip))
    peer_number = int(ipaddress.IPv4Address(peer_ip))
    if local_number == peer_number:
        raise ValueError(f"local and peer IPs must differ: {local_ip}")
    return min(local_number, peer_number), max(local_number, peer_number)


def select_links(peer_mapping: dict[str, str],
                 discovered: dict[str, tuple[str, list[tuple[str, int]]]]) -> list[Link]:
    """Pair each configured peer IP with one local RDMA interface address."""
    if not peer_mapping:
        raise ValueError("peer profile has no RDMA device mappings")
    if len(set(peer_mapping.values())) != len(peer_mapping):
        raise ValueError("peer profile maps multiple RDMA devices to one peer IP")
    links: list[Link] = []
    for device_name, peer_ip in peer_mapping.items():
        device_addresses = discovered.get(device_name)
        if device_addresses is None:
            raise ValueError(f"configured RDMA device is not UP with IPv4: {device_name}")
        netdev, local_addresses = device_addresses
        matching_addresses = [
            local_ip
            for local_ip, prefix_length in local_addresses
            if ipaddress.IPv4Address(peer_ip) in ipaddress.IPv4Network(
                f"{local_ip}/{prefix_length}", strict=False)
        ]
        if len(matching_addresses) != 1:
            raise ValueError(
                f"expected one local IPv4 address on {device_name}/{netdev} "
                f"matching peer {peer_ip}; found {matching_addresses}")
        local_ip = matching_addresses[0]
        canonical_link_key(local_ip, peer_ip)
        links.append(Link(device_name, netdev, local_ip, peer_ip, 0))
    links.sort(key=lambda link: canonical_link_key(link.local_ip, link.peer_ip))
    keys = [canonical_link_key(link.local_ip, link.peer_ip) for link in links]
    if len(set(keys)) != len(keys):
        raise ValueError("multiple RDMA devices resolve to the same local/peer IP pair")
    return [Link(link.device, link.netdev, link.local_ip, link.peer_ip, index)
            for index, link in enumerate(links)]


def validate_port_range(arguments: TransferArguments, link_count: int) -> None:
    """Ensure every link/mode pair receives a distinct valid TCP port."""
    port_count = link_count * len(arguments.modes)
    if arguments.base_port + port_count - 1 > 65535:
        raise ValueError(
            f"--base-port plus {port_count} link/mode ports exceeds 65535")


def locate_perftest(verb: str) -> str:
    """Find installed perftest and verify required options."""
    executable_name = f"ib_{verb}_bw"
    executable = shutil.which(executable_name)
    if executable is None:
        raise ValueError(f"required executable not found: {executable_name}")
    try:
        help_result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"cannot inspect {executable_name} options: {error}") from error
    help_output = help_result.stdout + help_result.stderr
    if "--bind_source_ip" not in help_output:
        raise ValueError(
            f"{executable_name} does not support --bind_source_ip")
    if "--noPeak" not in help_output and "-N" not in help_output:
        raise ValueError(f"{executable_name} does not support -N/--noPeak")
    if "--report_gbits" not in help_output:
        raise ValueError(f"{executable_name} does not support --report_gbits")
    return executable


def test_port(arguments: TransferArguments, link: Link, mode: str) -> int:
    """Calculate the same deterministic port on both ends of a link."""
    mode_index = arguments.modes.index(mode)
    return arguments.base_port + link.port_index * len(arguments.modes) + mode_index


def create_command(arguments: TransferArguments, executable: str,
                   link: Link, mode: str) -> list[str]:
    """Build one fixed-volume host or client perftest command."""
    command = [
        executable,
        "-d", link.device,
        "--bind_source_ip", link.local_ip,
        "-i", "1",
        "-F",
        "-N",
        "-p", str(test_port(arguments, link, mode)),
        "-c", "RC",
        "-n", str(arguments.iterations),
        "-s", str(arguments.message_bytes),
        "--report_gbits",
    ]
    if arguments.role == "client":
        command.append(link.peer_ip)
    return command


def create_run_directory(arguments: TransferArguments) -> Path:
    """Create this role's run directory without overwriting prior results."""
    campaign_name, run_id = arguments.run_name.rsplit("/", maxsplit=1)
    run_directory = (arguments.results.resolve() / campaign_name
                     / arguments.role / run_id)
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ValueError(f"run directory already exists: {run_directory}") from error
    return run_directory


def write_run_metadata(run_directory: Path, node_name: str, peer_name: str,
                       arguments: TransferArguments, links: list[Link]) -> None:
    """Write endpoint, workload, and command metadata for this run."""
    metadata_path = run_directory / "run.json"
    metadata = {
        "run_name": arguments.run_name,
        "role": arguments.role,
        "architecture": platform.machine(),
        "node": node_name,
        "peer": peer_name,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "modes": list(arguments.modes),
        "verb": arguments.verb,
        "transfer_bytes": arguments.transfer_bytes,
        "message_bytes": arguments.message_bytes,
        "iterations": arguments.iterations,
        "data_phase_time_estimate": {
            "source": "perftest-reported average bandwidth",
            "calculation": (
                "transfer_bytes * 8 / (reported_average_Gbit_s * 1e9)"
            ),
            "rounding_bound": (
                "derived from the displayed bandwidth precision, assuming "
                "round-to-nearest"
            ),
        },
        "client_process_wall_elapsed_s_includes": [
            "process startup", "RDMA connection setup", "data transfer",
            "process teardown",
        ],
        "links": [
            {
                "device": link.device,
                "netdev": link.netdev,
                "local_ip": link.local_ip,
                "peer_ip": link.peer_ip,
                "port_index": link.port_index,
            }
            for link in links
        ],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")


def write_commands(run_directory: Path, arguments: TransferArguments,
                   executable: str, links: list[Link]) -> None:
    """Save every exact perftest command for this role."""
    lines: list[str] = []
    for mode in arguments.modes:
        for link in links:
            command = create_command(arguments, executable, link, mode)
            lines.append(
                f"mode={mode} device={link.device} port={test_port(arguments, link, mode)}")
            lines.append(shlex.join(command))
    (run_directory / "commands.txt").write_text("\n".join(lines) + "\n")


def launch_test(arguments: TransferArguments, executable: str,
                run_directory: Path, link: Link, mode: str
                ) -> RunningTest:
    """Start one perftest process and send its output to a raw log."""
    command = create_command(arguments, executable, link, mode)
    log_path = run_directory / f"ib_{arguments.verb}_bw_{link.device}_{mode}.log"
    log_file = log_path.open("wb")
    started_ns = time.perf_counter_ns()
    environment: dict[str, str] = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    except OSError:
        log_file.close()
        raise
    return RunningTest(mode, link, log_path, process, log_file, started_ns)


def wait_for_test(test: RunningTest, timeout_seconds: int
                  ) -> tuple[str, float]:
    """Wait for perftest and return status plus client process wall time."""
    elapsed_before_wait = (time.perf_counter_ns() - test.started_ns) / 1_000_000_000
    remaining_seconds = timeout_seconds - elapsed_before_wait
    try:
        return_code = test.process.wait(timeout=max(0, remaining_seconds))
        status = "ok" if return_code == 0 else f"exit_{return_code}"
    except subprocess.TimeoutExpired:
        try:
            test.process.terminate()
        except ProcessLookupError:
            pass
        try:
            test.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            test.process.kill()
            test.process.wait()
        status = "timeout"
    elapsed_seconds = (time.perf_counter_ns() - test.started_ns) / 1_000_000_000
    test.log_file.close()
    return status, elapsed_seconds


def stop_running_tests(tests: list[RunningTest]) -> None:
    """Terminate suite-owned processes that remain after an error or interrupt."""
    for test in tests:
        if test.process.poll() is None:
            try:
                test.process.terminate()
            except ProcessLookupError:
                pass
    for test in tests:
        if test.process.poll() is None:
            try:
                test.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                test.process.kill()
                test.process.wait()
        if not test.log_file.closed:
            test.log_file.close()


def read_average_bandwidth_gbps(log_path: Path) -> Decimal:
    """Read perftest's average bandwidth and preserve its printed precision."""
    try:
        log_lines = log_path.read_text(errors="replace").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read perftest log {log_path}: {error}") from error

    header_positions = [
        line_index
        for line_index, line in enumerate(log_lines)
        if PERFTEST_BANDWIDTH_HEADER_PATTERN.search(line)
    ]
    if len(header_positions) != 1:
        raise ValueError(
            f"expected one perftest average-bandwidth header in {log_path}; "
            f"found {len(header_positions)}")

    header_index = header_positions[0]
    header = log_lines[header_index]
    average_header_match = PERFTEST_BANDWIDTH_HEADER_PATTERN.search(header)
    if average_header_match is None:
        raise ValueError(f"cannot locate average-bandwidth column in {log_path}")
    peak_header_match = PERFTEST_PEAK_BANDWIDTH_HEADER_PATTERN.search(header)
    average_column_index = 2
    if (peak_header_match is not None
            and peak_header_match.start() < average_header_match.start()):
        average_column_index += 1

    reported_values: list[Decimal] = []
    for line in log_lines[header_index + 1:]:
        fields = line.split()
        if (len(fields) <= average_column_index
                or not fields[0].isdigit()
                or not fields[1].isdigit()
                or PERFTEST_NUMBER_PATTERN.fullmatch(
                    fields[average_column_index]) is None):
            continue
        try:
            average_bandwidth_gbps = Decimal(fields[average_column_index])
        except InvalidOperation as error:
            raise ValueError(
                f"invalid average bandwidth in {log_path}: "
                f"{fields[average_column_index]!r}") from error
        if not average_bandwidth_gbps.is_finite() or average_bandwidth_gbps <= 0:
            raise ValueError(
                f"perftest reported a non-positive or non-finite average "
                f"bandwidth in {log_path}: {average_bandwidth_gbps}")
        reported_values.append(average_bandwidth_gbps)

    if len(reported_values) != 1:
        raise ValueError(
            f"expected one perftest average-bandwidth result in {log_path}; "
            f"found {len(reported_values)}")
    return reported_values[0]


def estimate_data_phase_time(transfer_bytes: int,
                             reported_average_bandwidth_gbps: Decimal
                             ) -> tuple[Decimal, Decimal]:
    """Convert reported bandwidth to time and bound output-rounding error."""
    payload_bits = Decimal(transfer_bytes * BITS_PER_BYTE)
    gigabits_per_second = Decimal(BITS_PER_GIGABIT)
    bandwidth_exponent = reported_average_bandwidth_gbps.as_tuple().exponent
    if not isinstance(bandwidth_exponent, int):
        raise ValueError("perftest's reported bandwidth has an invalid exponent")
    displayed_bandwidth_step_gbps = Decimal(1).scaleb(bandwidth_exponent)
    half_step_gbps = displayed_bandwidth_step_gbps / 2
    lower_bandwidth_gbps = reported_average_bandwidth_gbps - half_step_gbps
    upper_bandwidth_gbps = reported_average_bandwidth_gbps + half_step_gbps
    if lower_bandwidth_gbps <= 0:
        raise ValueError(
            "perftest's displayed bandwidth precision is too coarse to bound "
            "the derived transfer time")

    estimated_seconds = payload_bits / (
        reported_average_bandwidth_gbps * gigabits_per_second)
    minimum_seconds = payload_bits / (upper_bandwidth_gbps * gigabits_per_second)
    maximum_seconds = payload_bits / (lower_bandwidth_gbps * gigabits_per_second)
    rounding_bound_seconds = max(
        estimated_seconds - minimum_seconds,
        maximum_seconds - estimated_seconds)
    return estimated_seconds, rounding_bound_seconds


def write_client_measurement(arguments: TransferArguments,
                             test: RunningTest,
                             process_wall_elapsed_seconds: float,
                             reported_average_bandwidth_gbps: Decimal | None,
                             estimated_data_phase_elapsed_seconds: Decimal | None,
                             estimated_data_phase_rounding_bound_seconds: Decimal | None,
                             status: str,
                             writer: csv.DictWriter[str]) -> None:
    """Write a bandwidth-derived data-phase time and process-wall diagnostic."""
    estimated_data_phase_elapsed_seconds_text = ""
    estimated_data_phase_rounding_bound_seconds_text = ""
    data_phase_goodput = ""
    reported_average_bandwidth_gbps_text = ""
    if (reported_average_bandwidth_gbps is not None
            and estimated_data_phase_elapsed_seconds is not None
            and estimated_data_phase_rounding_bound_seconds is not None):
        reported_average_bandwidth_gbps_text = str(
            reported_average_bandwidth_gbps)
        estimated_data_phase_elapsed_seconds_text = format(
            estimated_data_phase_elapsed_seconds, ".12g")
        estimated_data_phase_rounding_bound_seconds_text = format(
            estimated_data_phase_rounding_bound_seconds, ".12g")
        data_phase_goodput = format(
            reported_average_bandwidth_gbps / Decimal(BITS_PER_BYTE), ".12g")
    writer.writerow({
        "run_name": arguments.run_name,
        "mode": test.mode,
        "verb": arguments.verb,
        "device": test.link.device,
        "local_ip": test.link.local_ip,
        "peer_ip": test.link.peer_ip,
        "transfer_bytes": arguments.transfer_bytes,
        "message_bytes": arguments.message_bytes,
        "iterations": arguments.iterations,
        "perftest_reported_average_Gbit_s": reported_average_bandwidth_gbps_text,
        "estimated_data_phase_elapsed_s": estimated_data_phase_elapsed_seconds_text,
        "estimated_data_phase_rounding_bound_s": (
            estimated_data_phase_rounding_bound_seconds_text),
        "data_phase_GBps": data_phase_goodput,
        "client_process_wall_elapsed_s": f"{process_wall_elapsed_seconds:.9f}",
        "status": status,
    })


def run_host(arguments: TransferArguments, executable: str,
             run_directory: Path, links: list[Link]) -> int:
    """Serve isolation links in order and concurrent links as a group."""
    failed_tests = 0
    for mode in arguments.modes:
        running_tests: list[RunningTest] = []
        try:
            for link in links:
                test = launch_test(arguments, executable, run_directory, link, mode)
                running_tests.append(test)
                print(f"host ready: {link.device} {mode} {test.log_path}", flush=True)
                if mode == "isolation":
                    status, _elapsed_seconds = wait_for_test(test, arguments.timeout)
                    if status != "ok":
                        failed_tests += 1
                        print(f"host {test.link.device} {test.mode}: {status}",
                              file=sys.stderr)
                    stop_running_tests([test])
                    running_tests.remove(test)
            for test in running_tests:
                status, _elapsed_seconds = wait_for_test(test, arguments.timeout)
                if status != "ok":
                    failed_tests += 1
                    print(f"host {test.link.device} {test.mode}: {status}",
                          file=sys.stderr)
        finally:
            stop_running_tests(running_tests)
    return 1 if failed_tests else 0


def run_client_mode(arguments: TransferArguments, executable: str,
                    run_directory: Path, links: list[Link], mode: str,
                    writer: csv.DictWriter[str]) -> int:
    """Run isolation links sequentially or concurrent links in parallel."""
    if mode == "isolation":
        failed_tests = 0
        for link in links:
            if arguments.initial_delay:
                time.sleep(arguments.initial_delay)
            test = launch_test(arguments, executable, run_directory, link, mode)
            try:
                status, elapsed_seconds = wait_for_test(test, arguments.timeout)
                reported_average_bandwidth_gbps: Decimal | None = None
                estimated_data_phase_elapsed_seconds: Decimal | None = None
                estimated_data_phase_rounding_bound_seconds: Decimal | None = None
                if status == "ok":
                    try:
                        reported_average_bandwidth_gbps = (
                            read_average_bandwidth_gbps(test.log_path))
                        (estimated_data_phase_elapsed_seconds,
                         estimated_data_phase_rounding_bound_seconds) = (
                            estimate_data_phase_time(
                                arguments.transfer_bytes,
                                reported_average_bandwidth_gbps))
                    except ValueError as error:
                        status = "invalid_bandwidth_report"
                        print(error, file=sys.stderr)
                write_client_measurement(
                    arguments, test, elapsed_seconds,
                    reported_average_bandwidth_gbps,
                    estimated_data_phase_elapsed_seconds,
                    estimated_data_phase_rounding_bound_seconds, status, writer)
                if (estimated_data_phase_elapsed_seconds is not None
                        and estimated_data_phase_rounding_bound_seconds is not None):
                    data_phase_summary = (
                        f"{format(estimated_data_phase_elapsed_seconds, '.9g')} s "
                        f"estimated data phase (±"
                        f"{format(estimated_data_phase_rounding_bound_seconds, '.3g')} s "
                        "report rounding); ")
                else:
                    data_phase_summary = "estimated data phase unavailable; "
                print(f"client {link.device} {mode}: {status}; "
                      f"{data_phase_summary}{elapsed_seconds:.6f} s process wall; "
                      f"{test.log_path}")
                if status != "ok":
                    failed_tests += 1
            finally:
                stop_running_tests([test])
        return failed_tests

    if arguments.initial_delay:
        time.sleep(arguments.initial_delay)
    running_tests: list[RunningTest] = []
    failed_tests = 0
    try:
        for link in links:
            running_tests.append(
                launch_test(arguments, executable, run_directory, link, mode))
        for test in running_tests:
            status, elapsed_seconds = wait_for_test(test, arguments.timeout)
            reported_average_bandwidth_gbps: Decimal | None = None
            estimated_data_phase_elapsed_seconds: Decimal | None = None
            estimated_data_phase_rounding_bound_seconds: Decimal | None = None
            if status == "ok":
                try:
                    reported_average_bandwidth_gbps = (
                        read_average_bandwidth_gbps(test.log_path))
                    (estimated_data_phase_elapsed_seconds,
                     estimated_data_phase_rounding_bound_seconds) = (
                        estimate_data_phase_time(
                            arguments.transfer_bytes,
                            reported_average_bandwidth_gbps))
                except ValueError as error:
                    status = "invalid_bandwidth_report"
                    print(error, file=sys.stderr)
            write_client_measurement(
                arguments, test, elapsed_seconds,
                reported_average_bandwidth_gbps,
                estimated_data_phase_elapsed_seconds,
                estimated_data_phase_rounding_bound_seconds, status, writer)
            if (estimated_data_phase_elapsed_seconds is not None
                    and estimated_data_phase_rounding_bound_seconds is not None):
                data_phase_summary = (
                    f"{format(estimated_data_phase_elapsed_seconds, '.9g')} s "
                    f"estimated data phase (±"
                    f"{format(estimated_data_phase_rounding_bound_seconds, '.3g')} s "
                    "report rounding); ")
            else:
                data_phase_summary = "estimated data phase unavailable; "
            print(f"client {test.link.device} {mode}: {status}; "
                  f"{data_phase_summary}{elapsed_seconds:.6f} s process wall; "
                  f"{test.log_path}")
            if status != "ok":
                failed_tests += 1
        return failed_tests
    finally:
        stop_running_tests(running_tests)


def run_client(arguments: TransferArguments, executable: str,
               run_directory: Path, links: list[Link]) -> int:
    """Run each configured mode and save bandwidth-derived time estimates."""
    measurement_path = run_directory / "measurements.csv"
    failed_tests = 0
    with measurement_path.open("w", newline="") as measurement_file:
        writer = csv.DictWriter(measurement_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for mode in arguments.modes:
            print(f"starting client mode: {mode}")
            failed_tests += run_client_mode(
                arguments, executable, run_directory, links, mode, writer)
            measurement_file.flush()
    return 1 if failed_tests else 0


def main() -> int:
    """Run one campaign iteration over the selected links and modes."""
    arguments = parse_arguments()
    try:
        node_name, peer_name, peer_mapping = load_peer_mapping(
            arguments.config, arguments.peer)
        discovered = discover_device_addresses()
        links = select_links(peer_mapping, discovered)
        validate_port_range(arguments, len(links))
        executable = locate_perftest(arguments.verb)
        run_directory = create_run_directory(arguments)
        write_run_metadata(run_directory, node_name, peer_name, arguments, links)
        write_commands(run_directory, arguments, executable, links)
        print(f"role={arguments.role} node={node_name} peer={peer_name} "
              f"modes={','.join(arguments.modes)} out={run_directory}")
        for link in links:
            print(f"  {link.device} | {link.local_ip} -> {link.peer_ip} "
                  f"| {link.netdev}")
        if arguments.role == "host":
            return run_host(arguments, executable, run_directory, links)
        return run_client(arguments, executable, run_directory, links)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
