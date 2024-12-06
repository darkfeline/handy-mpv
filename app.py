#!/usr/bin/python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import io
import json
import logging
import os
import sys
import time
import typing

if typing.TYPE_CHECKING:
    from typing import Optional

import mpv
import requests

import config

HOUR_NS = 3600_000_000_000

logger = logging.getLogger(__name__)

def time_ms() -> int:
    return int(time.time_ns() / 1000000)

class HandyClient:

    API_ENDPOINT="https://www.handyfeeling.com/api/handy/v2/"

    def __init__(self, api_secret: str):
        self.headers = {'X-Connection-Key': api_secret}

    def servertime(self) -> int:
        r = requests.get(f'{self.API_ENDPOINT}servertime', headers=self.headers)
        data = json.loads(r.text)
        return data['serverTime']

    def upload_script(self, path: str) -> None:
        r = requests.post("https://tugbud.kaffesoft.com/cache", files={'file': open(path, 'rb')})
        data = json.loads(r.text)
        logger.debug('Got response from cache %r', data)
        r = requests.put(f'{self.API_ENDPOINT}hssp/setup', json={'url': data['url']}, headers=self.headers)
        data = json.loads(r.text)

    def status(self) -> dict:
        r = requests.get(f'{self.API_ENDPOINT}status', headers=self.headers)
        return json.loads(r.text)

    def set_mode(self, mode: int) -> None:
        r = requests.put(f'{self.API_ENDPOINT}mode', json={"mode": mode}, headers=self.headers)
        logger.debug('Got response from set mode: %r', r.text)

    def stop(self) -> None:
        r = requests.put(f'{self.API_ENDPOINT}hssp/stop', headers=self.headers)

    def play(self, obj: dict) -> None:
        r = requests.put(f'{self.API_ENDPOINT}hssp/play', json=obj, headers=self.headers)
        logger.debug('Got response from play: %r', r.text)

@dataclass
class TimeSyncInfo:
    last_saved: int = 0
    average_offset: float = 0
    initial_offset: int = 0

    @staticmethod
    def from_file(path: str) -> TimeSyncInfo:
        with open(path, 'r') as f:
            obj = json.load(f)
            return TimeSyncInfo(
                last_saved=obj['last_saved'],
                average_offset=obj['time_sync_average_offset'],
                initial_offset=obj['time_sync_initial_offset'],
            )

    def write_to(self, path: str) -> None:
        if not os.path.exists(path):
            fp = open(path, 'x')
            fp.close()
        with open(path, 'w') as f:
            json.dump({
                'last_saved': self.last_saved,
                'time_sync_average_offset': self.average_offset,
                'time_sync_initial_offset': self.initial_offset,
            }, f)

    def newer_than(self, time_ns: int) -> bool:
        return self.last_saved > time_ns

class TimeSyncer:

    def __init__(self):
        self.aggregate_offset: int = 0
        self.sync_count: int = 0
        self.average_offset: float = 0
        self.initial_offset: int = 0

    def save_to(self, path: str) -> None:
        tsi = TimeSyncInfo(
                last_saved=time.time_ns(),
                average_offset=self.average_offset,
                initial_offset=self.initial_offset,
        )
        tsi.write_to(path)

    def load(self, tsi: TimeSyncInfo) -> None:
        self.average_offset = tsi.average_offset
        self.initial_offset = tsi.initial_offset

    def get_server_time(self) -> int:
        return int(time_ms() + self.average_offset + self.initial_offset)

    def update_server_time(self, client: HandyClient) -> None:
        send_time = time_ms()
        server_time = client.servertime()
        print(server_time)
        time_now = time_ms()
        print(time_now)
        rtd = time_now - send_time
        estimated_server_time_now = int(server_time + rtd / 2)

        # this part here, real dumb.
        if self.sync_count == 0:
            self.initial_offset = estimated_server_time_now - time_now
            print(f'initial offset {self.initial_offset} ms')
        else:
            offset = estimated_server_time_now - time_now - self.initial_offset
            self.aggregate_offset += offset
            self.average_offset = self.aggregate_offset / self.sync_count

        self.sync_count += 1
        if self.sync_count < 30:
            self.update_server_time(client)
        else:
            print(f'we in sync, Average offset is: {int(self.average_offset)} ms')
            return

    def update_with_file(self, sync_file: str, client: HandyClient) -> None:
        if os.path.exists(sync_file):
            tsi = TimeSyncInfo.from_file(sync_file)
        else:
            tsi = TimeSyncInfo()

        if tsi.newer_than(time.time_ns() - HOUR_NS):
            self.load(tsi)
        else:
            self.update_server_time(client)
            self.save_to(sync_file)

class HandyPlayer:

    def __init__(self, *, client: HandyClient, syncer: TimeSyncer):
        self.client = client
        self.syncer = syncer

    def sync_play(self, time: int, *, stopped: bool = False):
        payload = {
            'estimatedServerTime': self.syncer.get_server_time(),
            'startTime': time,
        }

        if stopped:
            self.client.stop()
        else:
            self.client.play(payload)

def find_script(video_path: str) -> str:
    video_name = video_path.replace('.' + str.split(video_path, '.')[-1:][0], '')
    script_path = f'{video_name}.funscript'
    if (os.path.exists(script_path)):
        print(f'script found for video: {video_name}')
    return script_path


logging.basicConfig(level=logging.DEBUG)
parser = argparse.ArgumentParser(description='Handy MPV sync Utility')
parser.add_argument('file', metavar='file', type=str,
                   help='The file to play')
args = parser.parse_args()
print(args)
script = find_script(args.file)

client = HandyClient(config.API_SECRET)

print('Getting Handy Status')
data = client.status()

if not data['mode']:
    print('Couldn\'t Sync with Handy, Exiting.')
    exit()

if data['mode'] != 1:
    client.set_mode(1)

print('Handy connected!')

syncer = TimeSyncer()
hplayer = HandyPlayer(client=client, syncer=syncer)

print('Uploading script!')

client.upload_script(script)


syncer.update_with_file(config.TIME_SYNC_FILE, client)

player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, osc=True)
player.play(args.file)

def get_playback_time(player) -> Optional[float]:
    value = player._get_property('playback-time')
    assert isinstance(value, float) or value is None
    return value

# @player.on_key_press('up')
def my_up_binding(key_state, key_name, key_char):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    print(time_ms)
    hplayer.sync_play(time_ms, stopped=True)

# @player.on_key_press('q')
def my_q_binding(key_state, key_name, key_char):
    global player
    hplayer.sync_play(0, stopped=True)
    player.command("quit")
    del player
    os._exit(-1)

# @player.on_key_press('down')
def my_down_binding(key_state, key_name, key_char):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    print(time_ms)
    hplayer.sync_play(time_ms)


player.register_key_binding("up", my_up_binding)
player.register_key_binding("q", my_q_binding)
player.register_key_binding("down", my_down_binding)

# @player.event_callback('playback-restart')
def file_restart(event):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    print(time_ms)
    hplayer.sync_play(time_ms)
    print(f'Now playing at {time_ms}s')

# @player.event_callback('shutdown')
def callback_shutdown(event):
    hplayer.sync_play(0, stopped=True)
    player.command("quit")
    sys.exit()

#@player.event_callback('pause')
def video_pause(event):
    hplayer.sync_play(0, stopped=True)

def video_pause_unpause(property_name, new_value):
    paused = new_value
    if paused:
        hplayer.sync_play(0, stopped=True)
    else:
        value = get_playback_time(player)
        if value is not None:
            time_ms = int(value * 1000)
            hplayer.sync_play(time_ms)

player.observe_property('pause', video_pause_unpause)

#@player.event_callback('unpause')
def video_unpause(event):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    hplayer.sync_play(time_ms)


def on_event(event):
    e = event.as_dict(decoder=mpv.lazy_decoder)["event"]
    match e:
        case "playback-restart":
            file_restart(event)
        case "shutdown":
            callback_shutdown(event)
        # case "pause":
        #     video_pause(event)
        # case "unpause":
        #     video_unpause(event)

player.register_event_callback(on_event)


try:
    player.wait_for_playback()
except mpv.ShutdownError as e:
    hplayer.sync_play(0, stopped=True)
    del player
    exit()
