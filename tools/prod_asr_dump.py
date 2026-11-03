"""把生产容器里的 ASR 留证（音频指纹日志 + asr_*.wav）取出来。

为什么要单独一个脚本：取证这件事必须**一条命令完成**。等现场复现了再去拼
``plink … docker logs | grep`` / ``docker cp`` 这种命令，最容易在着急的时候
抄错容器名或路径，把证据时间窗错过（容器重建一次日志就没了，见 MEMORY 第 10 条）。

用法（在仓库根目录）：

    python tools/prod_asr_dump.py            # 只列：指纹日志 + 现有 wav 清单
    python tools/prod_asr_dump.py --fetch    # 再把最新的 wav 拉到 build-out/

凭据：优先读环境变量 ``ELDER_SSH_PW``；没有就从 ``G:/workbuddy/github-token.txt``
第 12 行（"服务器SSH … 密码 …"）里取。**不要把密码写进本文件**。

读什么：
  * ``ASR 返回空，音频指纹：…``   —— VAD 认为说了话、ASR 却返回空
  * ``…重新武装而不送 ASR；音频指纹：…`` —— 整个收听窗口一句都没判成语音
    （用户说的"唤醒后完全没识别、一直挂着聆听"多半是这一条）
  两种都带 ``rms= / peak= / 时长= / 语音=``，先看这几个数就能把
  「太小 / 全是噪声 / 被切成 12 段」分开，不必等 wav 下载完。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HOST = "152.136.230.132"
USER = "root"
HOSTKEY = "SHA256:tqM2jMQJYUmcxPeG+vkFCu8tDFDqeUIgsfNnwKCMVTI"
TOKEN_FILE = Path("G:/workbuddy/github-token.txt")
CONTAINER = "elder-server"
OUT_DIR = Path("build-out")


def ssh_password() -> str:
    pw = os.getenv("ELDER_SSH_PW", "").strip()
    if pw:
        return pw
    if not TOKEN_FILE.is_file():
        sys.exit(f"找不到凭据文件 {TOKEN_FILE}，也没有设置 ELDER_SSH_PW")
    for line in TOKEN_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "密码" in line and "root" in line:
            m = re.search(r"密码\s*[:：]?\s*(\S+)", line)
            if m:
                return m.group(1)
    sys.exit("凭据文件里没找到『服务器SSH … 密码 …』那一行")


def run(remote: str, password: str) -> str:
    cmd = ["plink", "-ssh", "-batch", "-hostkey", HOSTKEY,
           "-pw", password, f"{USER}@{HOST}", remote]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        sys.exit(f"远端命令失败（{proc.returncode}）：{proc.stderr.strip()[:400]}")
    return proc.stdout


def main() -> int:
    fetch = "--fetch" in sys.argv
    pw = ssh_password()

    print("== 1. 音频指纹（最近 20 条）==")
    logs = run(f"docker logs {CONTAINER} 2>&1 | grep '音频指纹' | tail -20", pw)
    if logs.strip():
        print(logs.rstrip())
    else:
        print("  （还没有。说明自上次重启以来没出现过『说了话却没识别』）")

    print("\n== 2. 留证文件 ==")
    listing = run(
        f"docker exec {CONTAINER} sh -c "
        f"'ls -l /data/asr_*.wav 2>/dev/null || echo 空'", pw)
    print(listing.rstrip() or "  空")

    if not fetch:
        print("\n提示：加 --fetch 把最新的 wav 拉到 build-out/ 并打印指纹。")
        return 0

    if "空" in listing or ".wav" not in listing:
        print("\n没有 wav 可拉。")
        return 0

    newest = run(
        f"docker exec {CONTAINER} sh -c "
        f"'ls -t /data/asr_*.wav | head -1'", pw).strip().splitlines()[-1].strip()
    OUT_DIR.mkdir(exist_ok=True)
    local = OUT_DIR / Path(newest).name
    run(f"docker cp {CONTAINER}:{newest} /root/{Path(newest).name}", pw)
    # plink 不能直接回传文件，先落到宿主再取：用 scp 语义最稳。
    subprocess.run(["pscp", "-batch", "-hostkey", HOSTKEY, "-pw", pw,
                    f"{USER}@{HOST}:/root/{Path(newest).name}", str(local)],
                   check=False)
    if local.is_file():
        size = local.stat().st_size
        print(f"\n已取回 {local}（{size} 字节 ≈ {size / 32000:.1f} 秒 16k/mono）")
        print("用任意播放器打开就能听：是「太轻」「全是噪声」还是「被切碎成 12 段」，")
        print("一听就分得清 —— 这三种原因的修法完全不同。")
    else:
        print(f"\n远端已复制到 {USER}@{HOST}:/root/{Path(newest).name}，"
              f"但 pscp 没取回来。手工：pscp {USER}@{HOST}:/root/{Path(newest).name} .")
    return 0


if __name__ == "__main__":
    sys.exit(main())
