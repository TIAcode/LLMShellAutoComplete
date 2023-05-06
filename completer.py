from collections import defaultdict
import json
from typing import Dict, List
import openai
import subprocess
import sys
import sqlite3
import os
import logging
import argparse

import asyncio
import random
import re
import tiktoken

MAX_TOKENS = 3500
log_filename = os.path.join(os.path.dirname(__file__), ".completer.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S.%f",
    filename=log_filename,
    filemode="w",
)

REMOVE_PATTERNS = [
    # sometimes GPT replies with
    # 1. commandline
    # 2. commandline
    (re.compile(r"^\d+\.\s*(.*)$"), r"\1"), 
]

def open_atuin_db(filename: str):
    conn = sqlite3.connect(filename)
    conn.row_factory = sqlite3.Row
    return conn


def remove_extra_unicode_characters(content):
    clean_content = "".join([c for c in content if ord(c) < 128])
    return clean_content


def filter_paths(possibilities: List[str]) -> List[str]:
    return [os.path.normpath(p) for p in possibilities if os.path.exists(p)]


def count_text_tokens(content: str, model="gpt-4") -> int:
    """
    Counts the number of tokens required to send the given text.
    :param content: the text
    :return: the number of tokens required
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("gpt-3.5-turbo")
    return len(encoding.encode(content))


def count_tokens(messages, model="gpt-4") -> int:
    """
    Counts the number of tokens required to send the given messages.
    :param messages: the messages to send
    :return: the number of tokens required
    """

    # Thanks for https://github.com/n3d1117/chatgpt-telegram-bot/blob/main/bot/openai_helper.py
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("gpt-3.5-turbo")

    if "gpt-3.5" in model:
        tokens_per_message = (
            4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        )
        tokens_per_name = -1  # if there's a name, the role is omitted
    else:
        tokens_per_message = 3
        tokens_per_name = 1
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            num_tokens += len(encoding.encode(value))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


async def main():
    # Get OS user name for this user
    user = os.getlogin()
    default_atuin_db = "/home/" + user + "/.local/share/atuin/history.db"
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--atuin",
        "-a",
        type=str,
        default=default_atuin_db,
        help="Path to atuin history.db",
    )
    parser.add_argument("--dunst",action="store_true", help="Use dunstify to show notifications")
    # parser.add_argument(
    #     "--max-files",
    #     "-f",
    #     type=int,
    #     help="Maximum number of files to send in prompt (per directory)",
    # )
    # parser.add_argument(
    #     "--max-dirs",
    #     "-d",
    #     type=int,
    #     help="Maximum number of directories to send in prompt (per directory)",
    # )
    parser.add_argument(
        "--cwd-history",
        "-ch",
        type=int,
        default=10,
        help="Maximum number of cwd history to send in prompt",
    )
    parser.add_argument(
        "--process-history",
        "-ph",
        type=int,
        default=10,
        help="Maximum number of history entries for the same process to send in prompt",
    )
    parser.add_argument(
        "--session-history",
        "-sh",
        type=int,
        default=10,
        help="Maximum number of history entries for the same session to send in prompt",
    )
    parser.add_argument(
        "--cwd-process-history",
        "-cph",
        type=int,
        default=5,
        help="Maximum number of history entries for the same process in the same cwd to send in prompt",
    )
    # parser.add_argument(
    #     "--cwd-process-session-history",
    #     "-cpsh",
    #     type=int,
    #     default=10,
    #     help="Maximum number of history entries for the same process in the same cwd in the same session to send in prompt",
    # )
    parser.add_argument("--shell", "-s", type=str, default="nushell", help="Shell name. Only given to GPT")
    parser.add_argument("--wezterm", "-w", action="store_true")
    # parser.add_argument("--kitty", "-k", action="store_true")
    parser.add_argument("--model", "-m", default="gpt-3.5-turbo", help="GPT model to use, gpt-4 or got-3.5-turbo")
    parser.add_argument("commandline", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    ATUIN_SESSION = os.environ.get("ATUIN_SESSION")
    assert ATUIN_SESSION is not None, "ATUIN_SESSION is not set"
    dunst_id = random.randint(2183, 1000000)

    cwd = os.getcwd()
    cmdline = " ".join(args.commandline)
    target_process = cmdline.split(maxsplit=1)[0] if len(sys.argv) > 1 else ""

    term_content = ""

    if args.wezterm:
        wezterm_capture_process = subprocess.run(
            ["wezterm", "cli", "get-text"], capture_output=True
        )
        term_content = wezterm_capture_process.stdout.decode("utf-8")
    # elif args.kitty:
    #     kitty_capture_process = subprocess.run(
    #         ["kitty", "@", "get-text"], capture_output=True
    #     )
    #     term_content = kitty_capture_process.stdout.decode("utf-8")

    if term_content:
        cleaned_term_content = remove_extra_unicode_characters(term_content)
        logging.info(f"cleaned_term_content from {len(term_content)} -> {cleaned_term_content}")
        # Log token count as well
        logging.info(f"term_content token count: {count_text_tokens(term_content)}")
        logging.info(f"cleaned_term_content token count: {count_text_tokens(cleaned_term_content)}")
        term_content = "Terminal content:\n" + cleaned_term_content + "\n\n"
    # cwd = os.path.normpath(sys.argv[1])

    # TODO: doesn't handle paths with spaces
    target_paths = None
    # if args.max_files or args.max_dirs:
    #     target_paths = filter_paths(cmdline.split())
    #     if os.path.abspath(cwd) not in [os.path.abspath(p) for p in target_paths]:
    #         logging.debug("Adding cwd to target_paths")
    #         target_paths.append(cwd)


    #     path_files: Dict[str, List[str]] = defaultdict(list)
    #     for path in target_paths:
    #         logging.debug(f"Looking for files in {path}")
    #         for root, dirs, files in os.walk(path):
    #             for file in files:
    #                 path_files[path].append(file)
    #             for dir in dirs:
    #                 path_files[path].append(dir + "/")
    #         logging.debug(f"Files: {path_files[path]}")

    logging.info(f"target_process: {target_process}")
    logging.info(f"cwd: {cwd}")
    logging.info(f"cmdline: {cmdline}")
    logging.info(f"target_paths: {target_paths}")



    db = open_atuin_db(args.atuin)
    cursor = db.cursor()
    same_process_str = same_cwd_str = same_session_str = ""
    same_cwd_process1_str = same_cwd_process_session_str = ""
    if args.process_history > 0:
        same_process = []
        for entry in cursor.execute(
            "SELECT DISTINCT(command) as command FROM history WHERE command LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (target_process + "%", args.process_history),
        ):
            same_process.append(entry["command"])
        if same_process:
            same_process_str = "Latest calls for the same executable:\n"
            same_process_str += "\n".join(same_process) + "\n\n"

    if args.cwd_history:
        same_cwd = []
        for entry in cursor.execute(
            "SELECT * FROM history WHERE cwd = ? ORDER BY timestamp DESC LIMIT ?",
            (cwd, args.cwd_history),
        ):
            same_cwd.append(entry["command"])
        if same_cwd:
            same_cwd_str = "Latest calls in the same directory:\n"
            same_cwd_str += "\n".join(same_cwd) + "\n\n"

    if args.session_history:
        same_session = []
        for entry in cursor.execute(
            "SELECT * FROM history WHERE session = ? ORDER BY timestamp DESC LIMIT ?",
            (ATUIN_SESSION, args.session_history),
        ):
            same_session.append(entry["command"])
        if same_session:
            same_session_str = "Latest calls in the same session:\n"
            same_session_str += "\n".join(same_session) + "\n\n"

    if args.cwd_process_history:
        same_cwd_process = []
        for entry in cursor.execute(
            "SELECT * FROM history WHERE cwd = ? AND command LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (cwd, target_process + "%", args.cwd_process_history),
        ):
            same_cwd_process.append(entry["command"])
        if same_cwd_process:
            same_cwd_process_str = (
                "Latest calls in the same directory for the same process:\n"
            )
            same_cwd_process_str += "\n".join(same_cwd_process) + "\n\n"

    # if args.cwd_process_session_history:
    #     same_cwd_process_session = []
    #     for entry in cursor.execute(
    #         "SELECT * FROM history WHERE cwd = ? AND command LIKE ? AND session = ? ORDER BY timestamp DESC LIMIT ?",
    #         (
    #             cwd,
    #             target_process + "%",
    #             ATUIN_SESSION,
    #             args.cwd_process_session_history,
    #         ),
    #     ):
    #         same_cwd_process_session.append(entry["command"])
    #     if same_cwd_process_session:
    #         same_cwd_process_session_str = "Latest calls in the same directory for the same process in the same session:\n"
    #         same_cwd_process_session_str += "\n".join(same_cwd_process_session) + "\n\n"

    prompt_text = f"""User terminal information:
Shell: {args.shell}
{term_content}{same_session_str}{same_cwd_str}{same_process_str}{same_cwd_process_str}{same_cwd_process_session_str}
CWD: {cwd}
Current command line: {cmdline}"""

    prompt = [
        {
            "role": "system",
            "content": """You are an AI autocompleting for user's terminal session."""},
        {
            "role": "user", 
            "content": """I will give you information from user's terminal - screen content and history of related commands ran. You can use this information to complete the current command line.

You have to reply with only the full command lines, do not output anything else. Do not prepend the lines with line numbers. Reply with 1-5 completions. The returned lines should contain the user command line input, and you can also change the user input if needed.""",
        },
        # {
        #     "role": "user",
        #     "name": "example_user",
        #     "content": ""
        # }
        {"role": "user", "content": prompt_text},
    ]
    # print(prompt_text)
    logging.debug(f"Prompt: {prompt_text}")
    tokens = count_tokens(prompt, args.model)
    logging.info(f"Request tokens: {tokens}")
    if tokens > MAX_TOKENS:
        logging.error(f"Request tokens: {tokens} > {MAX_TOKENS}")
        subprocess.run(
            ["dunstify", "-r", str(dunst_id), f"Too many tokens, {tokens} > {MAX_TOKENS}", f"{cwd}\n\n{cmdline}"]
        )
        return
    subprocess.run(
        ["dunstify", "-r", str(dunst_id), f"Completing, {tokens} tokens", f"{cwd}\n\n{cmdline}"]
    )

    response = await openai.ChatCompletion.acreate(
        model=args.model, messages=prompt, stream=True
    )

    async def gen():
        async for chunk in response:
            if "choices" not in chunk or len(chunk["choices"]) == 0:
                continue
            yield chunk["choices"][0]["delta"].get("content", "")
        yield "\n"
    current_data = ""
    async for chunk in gen():
        # logging.info("ChunkResponse:\n",chunk)
        current_data += chunk
        while "\n" in current_data:
            line, current_data = current_data.split("\n", 1)
            if line:
                logging.debug(f"Response line: {line}")
                for pattern in REMOVE_PATTERNS:
                    line = pattern[0].sub(pattern[1], line)
                line=line.strip("`")
                logging.debug(f"Response line after patterns: {line}")
                print(line, flush=True)

        if not current_data:
            current_data = ""
    if current_data:
        line = current_data
        for pattern in REMOVE_PATTERNS:
            line = pattern[0].sub(pattern[1], line)
        line=line.strip("`")

        print(line, flush=True)

    # logging.info("Response:\n",response)
    # result = json.loads(response["choices"][0]["message"]["content"])
    # for x in response:
    #    logging.info("Response:\n","y"+x+"z")
    # result = json.loads(x["choices"][0]["message"]["content"])
    # if result:
    #    break
    subprocess.run(
        ["dunstify", "-r", str(dunst_id), f"Completion finished", f"{cmdline}"]
    )
    exit()
    result = json.loads(DEBUG_RESULT)
    print("\n".join([x["value"] for x in result]))
    # fzf_process = subprocess.run(["fzf", "--ansi", "--multi", "--preview-window", "up:50%", "--preview", "echo {}"], input="\n".join([x["value"] for x in result]).encode("utf-8"), capture_output=True)
    # print(fzf_process.stdout.decode("utf-8"))
    # fzf = FzfPrompt()
    # selection = fzf.prompt([x.get("value") for x in result]) # , preview_window="up:50%", preview="echo {}")
    # for x in result:
    #    print(x["value"])
    # if not selection:
    #    print(cmdline)
    # else:
    #    print(selection[0])


if __name__ == "__main__":
    asyncio.run(main())
