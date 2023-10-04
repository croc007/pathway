#!/usr/bin/env python

# Copyright © 2023 Pathway

import json
import os
import pathlib
import random
import shutil
import subprocess
import time
import uuid
import warnings

import boto3

DEFAULT_INPUT_SIZE = 5000000
COMMIT_LINE = "*COMMIT*\n"
STATIC_MODE_NAME = "static"
STREAMING_MODE_NAME = "streaming"
DICTIONARY = None
FS_STORAGE_NAME = "fs"
S3_STORAGE_NAME = "s3"


class PStoragePath:
    def __init__(self, pstorage_type, local_tmp_path: pathlib.Path):
        self._pstorage_type = pstorage_type
        self._pstorage_path = self._get_pstorage_path(pstorage_type, local_tmp_path)

    def __enter__(self):
        return self._pstorage_path

    def __exit__(self, exc_type, exc_value, traceback):
        if self._pstorage_type == "s3":
            self._clean_s3_prefix(self._pstorage_path)
        elif self._pstorage_type == "fs":
            shutil.rmtree(self._pstorage_path)
        else:
            raise NotImplementedError(
                f"method not implemented for storage {self._pstorage_type}"
            )

    @staticmethod
    def _get_pstorage_path(pstorage_type, local_tmp_path: pathlib.Path):
        if pstorage_type == "fs":
            return str(local_tmp_path / "pstorage")
        elif pstorage_type == "s3":
            return f"wordcount-integration-tests/pstorages/{time.time()}-{str(uuid.uuid4())}"
        else:
            raise NotImplementedError(
                f"method not implemented for storage {pstorage_type}"
            )

    @staticmethod
    def _clean_s3_prefix(prefix):
        s3 = boto3.client(
            "s3",
            aws_access_key_id=os.environ["AWS_S3_ACCESS_KEY"],
            aws_secret_access_key=os.environ["AWS_S3_SECRET_ACCESS_KEY"],
        )

        while True:
            objects_to_delete = s3.list_objects_v2(
                Bucket="aws-integrationtest", Prefix=prefix
            )
            if "Contents" in objects_to_delete and objects_to_delete["Contents"]:
                objects = [{"Key": obj["Key"]} for obj in objects_to_delete["Contents"]]
                s3.delete_objects(
                    Bucket="aws-integrationtest", Delete={"Objects": objects}
                )
            else:
                break


def check_output_correctness(
    latest_input_file, input_path, output_path, interrupted_run=False
):
    input_word_counts = {}
    new_file_lines = set()
    distinct_new_words = set()

    input_file_list = os.listdir(input_path)
    for processing_old_files in (True, False):
        for file in input_file_list:
            path = os.path.join(input_path, file)
            if not os.path.isfile(path):
                continue

            on_old_file = path != latest_input_file
            if on_old_file != processing_old_files:
                continue
            with open(path) as f:
                for row in f:
                    if not row.strip() or row.strip() == "*COMMIT*":
                        continue
                    json_payload = json.loads(row.strip())
                    word = json_payload["word"]
                    if word not in input_word_counts:
                        input_word_counts[word] = 0
                    input_word_counts[word] += 1

                    if not on_old_file:
                        new_file_lines.add((word, input_word_counts[word]))
                        distinct_new_words.add(word)

    print("  New file lines:", len(new_file_lines))

    n_rows = 0
    n_old_lines = 0
    output_word_counts = {}
    try:
        with open(output_path) as f:
            is_first_row = True
            word_column_index = None
            count_column_index = None
            for row in f:
                n_rows += 1
                if is_first_row:
                    column_names = row.strip().split(",")
                    for col_idx, col_name in enumerate(column_names):
                        if col_name == "word":
                            word_column_index = col_idx
                        elif col_name == "count":
                            count_column_index = col_idx
                    is_first_row = False
                    assert (
                        word_column_index is not None
                    ), "'word' is absent in CSV header"
                    assert (
                        count_column_index is not None
                    ), "'count' is absent in CSV header"
                    continue

                tokens = row.strip().split(",")
                try:
                    word = tokens[word_column_index].strip('"')
                    count = tokens[count_column_index]
                    output_word_counts[word] = int(count)
                except IndexError:
                    # line split in two chunks, one fsynced, another did not
                    if not interrupted_run:
                        raise

                if (word, int(count)) not in new_file_lines:
                    n_old_lines += 1
    except FileNotFoundError:
        if interrupted_run:
            return False
        raise

    assert len(input_word_counts) >= len(output_word_counts), (
        "There are some new words on the output. "
        + f"Input dict: {len(input_word_counts)} Output dict: {len(output_word_counts)}"
    )

    for word, output_count in output_word_counts.items():
        if interrupted_run:
            assert input_word_counts.get(word) >= output_count
        else:
            assert (
                input_word_counts.get(word) == output_count
            ), f"Word: {word} Output count: {output_count} Input count: {input_word_counts.get(word)}"

    if not interrupted_run:
        assert n_old_lines < DEFAULT_INPUT_SIZE / 10, (
            f"Output contains too many old lines: {n_old_lines} while 1/10 of the input size "
            + f"is {DEFAULT_INPUT_SIZE / 10}"
        )
        assert n_rows >= len(
            distinct_new_words
        ), f"Output contains only {n_rows} lines, while there should be at least {len(distinct_new_words)}"

    print("  Total rows on the output:", n_rows)
    print("  Total old lines:", n_old_lines)

    return input_word_counts == output_word_counts


def start_pw_computation(
    n_cpus, input_path, output_path, pstorage_path, mode, pstorage_type
):
    pw_wordcount_path = (
        "/".join(os.path.abspath(__file__).split("/")[:-1])
        + f"/pw_wordcount.py --input {input_path} --output {output_path} --pstorage {pstorage_path} "
        + f"--n-cpus {n_cpus} --mode {mode} --pstorage-type {pstorage_type}"
    )

    cpu_list = ",".join([str(x) for x in range(n_cpus)])
    command = f"taskset --cpu-list {cpu_list} python {pw_wordcount_path}"
    run_args = command.split()

    return subprocess.Popen(run_args)


def get_pw_program_run_time(
    n_cpus, input_path, output_path, pstorage_path, mode, pstorage_type
):
    needs_pw_program_launch = True
    n_retries = 0
    while needs_pw_program_launch:
        needs_pw_program_launch = False
        time_start = time.time()
        popen = start_pw_computation(
            n_cpus, input_path, output_path, pstorage_path, mode, pstorage_type
        )
        try:
            needs_polling = mode == STREAMING_MODE_NAME
            while needs_polling:
                print("Waiting for 10 seconds...")
                time.sleep(10)

                # Insert file size check here

                try:
                    modified_at = os.path.getmtime(output_path)
                    file_size = os.path.getsize(output_path)
                    if file_size == 0:
                        continue
                except FileNotFoundError:
                    if time.time() - time_start > 180:
                        raise
                    continue
                if modified_at > time_start and time.time() - modified_at > 60:
                    popen.kill()
                    needs_polling = False
        finally:
            if mode == STREAMING_MODE_NAME:
                pw_exit_code = popen.poll()
                if not pw_exit_code:
                    popen.kill()
            else:
                pw_exit_code = popen.wait()

            if pw_exit_code is not None and pw_exit_code != 0:
                warnings.warn(
                    f"Warning: pw program terminated with non zero exit code: {pw_exit_code}"
                )
                assert n_retries < 3, "Number of retries for S3 reconnection exceeded"
                needs_pw_program_launch = True
                n_retries += 1

    return time.time() - time_start


def run_pw_program_suddenly_terminate(
    n_cpus,
    input_path,
    output_path,
    pstorage_path,
    min_work_time,
    max_work_time,
    pstorage_type,
):
    popen = start_pw_computation(
        n_cpus, input_path, output_path, pstorage_path, STATIC_MODE_NAME, pstorage_type
    )
    try:
        wait_time = random.uniform(min_work_time, max_work_time)
        time.sleep(wait_time)
    finally:
        popen.kill()


def reset_runtime(inputs_path, output_path, pstorage_path, pstorage_type):
    if pstorage_type == "fs":
        try:
            shutil.rmtree(pstorage_path)
        except FileNotFoundError:
            print("There is no persistent storage to remove")
        except Exception:
            print("Failed to clean persistent storage")
            raise

    try:
        shutil.rmtree(inputs_path)
        os.remove(output_path)
    except FileNotFoundError:
        print("There is no inputs directory to remove")
    except Exception:
        print("Failed to clean inputs directory")
        raise

    os.makedirs(inputs_path)
    print("State successfully re-set")


def generate_word():
    word_chars = []
    for _ in range(10):
        word_chars.append(random.choice("abcdefghijklmnopqrstuvwxyz"))
    return "".join(word_chars)


def generate_dictionary(dict_size):
    result_as_set = set()
    for _ in range(dict_size):
        word = generate_word()
        while word in result_as_set:
            word = generate_word()
        result_as_set.add(word)
    return list(result_as_set)


DICTIONARY = generate_dictionary(10000)


def generate_input(file_name, input_size, commit_frequency):
    with open(file_name, "w") as fw:
        for seq_line_id in range(input_size):
            word = random.choice(DICTIONARY)
            dataset_line_dict = {"word": word}
            dataset_line = json.dumps(dataset_line_dict)
            fw.write(dataset_line + "\n")
            if (seq_line_id + 1) % commit_frequency == 0:
                fw.write(COMMIT_LINE)


def generate_next_input(inputs_path):
    file_name = os.path.join(inputs_path, str(time.time()))

    generate_input(
        file_name=file_name,
        input_size=DEFAULT_INPUT_SIZE,
        commit_frequency=100000,
    )

    return file_name


def do_test_persistent_wordcount(
    n_backfilling_runs, n_cpus, tmp_path, mode, pstorage_type
):
    inputs_path = tmp_path / "inputs"
    output_path = tmp_path / "table.csv"

    with PStoragePath(pstorage_type, tmp_path) as pstorage_path:
        reset_runtime(inputs_path, output_path, pstorage_path, pstorage_type)
        for n_run in range(n_backfilling_runs):
            print(f"Run {n_run}: generating input")
            latest_input_name = generate_next_input(inputs_path)

            print(f"Run {n_run}: running pathway program")
            elapsed = get_pw_program_run_time(
                n_cpus, inputs_path, output_path, pstorage_path, mode, pstorage_type
            )
            print(f"Run {n_run}: pathway time elapsed {elapsed}")

            print(f"Run {n_run}: checking output correctness")
            check_output_correctness(latest_input_name, inputs_path, output_path)
            print(f"Run {n_run}: finished")


def do_test_failure_recovery_static(
    n_backfilling_runs, n_cpus, tmp_path, min_work_time, max_work_time, pstorage_type
):
    inputs_path = tmp_path / "inputs"
    output_path = tmp_path / "table.csv"

    with PStoragePath(pstorage_type, tmp_path) as pstorage_path:
        reset_runtime(inputs_path, output_path, pstorage_path, pstorage_type)
        finished = False
        input_file_name = generate_next_input(inputs_path)
        for n_run in range(n_backfilling_runs):
            print(f"Run {n_run}: generating input")

            print(f"Run {n_run}: running pathway program")
            run_pw_program_suddenly_terminate(
                n_cpus,
                inputs_path,
                output_path,
                pstorage_path,
                min_work_time,
                max_work_time,
                pstorage_type,
            )

            finished_in_this_run = check_output_correctness(
                input_file_name, inputs_path, output_path, interrupted_run=True
            )
            if finished_in_this_run:
                finished = True

        if finished:
            print("The program finished during one of interrupted runs")
        else:
            elapsed = get_pw_program_run_time(
                n_cpus,
                inputs_path,
                output_path,
                pstorage_path,
                STATIC_MODE_NAME,
                pstorage_type,
            )
            print("Time elapsed for non-interrupted run:", elapsed)
            print("Checking correctness at the end")
            check_output_correctness(input_file_name, inputs_path, output_path)
