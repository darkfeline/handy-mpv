#!/usr/bin/python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import sys
import time
import tomllib
import typing

if typing.TYPE_CHECKING:
    from typing import Optional

import mpv
import requests

HOUR_NS = 3600_000_000_000

logger = logging.getLogger(__name__)

def time_ms() -> int:
    return int(time.time_ns() / 1000000)

@dataclass
class Config:
    api_secret: str
    time_sync_file: str

    @staticmethod
    def load() -> Config:
        config_dir = Path(os.getenv('XDG_CONFIG_HOME', Path.home() / '.config'))
        config_file = config_dir / 'handy-mpv.toml'
        with open(config_file, 'rb') as f:
            obj = tomllib.load(f)
        return Config(
            api_secret=obj['api_secret'],
            time_sync_file=obj['time_sync_file'],
        )

class HandyClient:

    API_ENDPOINT="https://www.handyfeeling.com/api/handy/v2/"

    def __init__(self, api_secret: str):
        self.headers = {'X-Connection-Key': api_secret}

    def servertime(self) -> int:
        logger.debug('servertime request')
        r = requests.get(f'{self.API_ENDPOINT}servertime', headers=self.headers)
        data = json.loads(r.text)
        logger.debug('servertime response: %r', data)
        return data['serverTime']

    def upload_script(self, path: str) -> None:
        logger.debug('upload_script request: %r', path)
        r = requests.post("https://tugbud.kaffesoft.com/cache", files={'file': open(path, 'rb')})
        data = json.loads(r.text)
        logger.debug('upload_script response: %r', data)
        r = requests.put(f'{self.API_ENDPOINT}hssp/setup', json={'url': data['url']}, headers=self.headers)
        data = json.loads(r.text)

    def status(self) -> dict:
        logger.debug('status request')
        r = requests.get(f'{self.API_ENDPOINT}status', headers=self.headers)
        data = json.loads(r.text)
        logger.debug('handyclient: status response: %r', data)
        return data

    def set_mode(self, mode: int) -> None:
        logger.debug('set_mode request')
        r = requests.put(f'{self.API_ENDPOINT}mode', json={"mode": mode}, headers=self.headers)
        data = json.loads(r.text)
        logger.debug('set_mode response: %r', data)

    def stop(self) -> None:
        logger.debug('stop request')
        r = requests.put(f'{self.API_ENDPOINT}hssp/stop', headers=self.headers)
        data = json.loads(r.text)
        logger.debug('stop response: %r', data)

    def play(self, obj: dict) -> None:
        logger.debug('play request')
        r = requests.put(f'{self.API_ENDPOINT}hssp/play', json=obj, headers=self.headers)
        data = json.loads(r.text)
        logger.debug('play response: %r', data)

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
        logger.debug('Updating server time')
        send_time = time_ms()
        server_time = client.servertime()
        logger.debug('Got server time %r', server_time)
        time_now = time_ms()
        logger.debug('Got current time %r', time_now)
        rtd = time_now - send_time
        estimated_server_time_now = int(server_time + rtd / 2)

        # this part here, real dumb.
        if self.sync_count == 0:
            self.initial_offset = estimated_server_time_now - time_now
            logger.debug('Got initial offset %r ms', self.initial_offset)
        else:
            offset = estimated_server_time_now - time_now - self.initial_offset
            self.aggregate_offset += offset
            self.average_offset = self.aggregate_offset / self.sync_count

        self.sync_count += 1
        if self.sync_count < 15:
            self.update_server_time(client)
        else:
            logger.debug('Synced, average offset: %r ms', self.average_offset)
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
        self.player: Optional[mpv.MPV] = None

    def sync_play(self, time: int, *, stopped: bool = False):
        logger.debug('sync_play: %r, %r', time, stopped)
        payload = {
            'estimatedServerTime': self.syncer.get_server_time(),
            'startTime': time,
        }

        if stopped:
            self.client.stop()
        else:
            self.client.play(payload)

    def attach_to(self, player: mpv.MPV):
        self.player = player
        player.register_key_binding("q", self._q_binding)
        player.register_key_binding("s", self._s_binding)

    def _q_binding(self, key_state, key_name, key_char):
        self.sync_play(0, stopped=True)
        assert self.player is not None
        self.player.command("quit")

    def _s_binding(self, key_state, key_name, key_char):
        time_ms = get_playback_time_ms(player)
        assert time_ms is not None
        self.sync_play(time_ms)

def find_script(video_path: str) -> str:
    video_name = video_path.replace('.' + str.split(video_path, '.')[-1:][0], '')
    script_path = f'{video_name}.funscript'
    return script_path

def get_playback_time_ms(player: mpv.MPV) -> Optional[int]:
    value = player._get_property('playback-time')
    if value is None:
        return value
    assert isinstance(value, float)
    return int(value * 1000)


logging.basicConfig(level=logging.DEBUG)
parser = argparse.ArgumentParser(description='Handy MPV sync Utility')
parser.add_argument('file', metavar='file', type=str,
                   help='The file to play')
args = parser.parse_args()
script = find_script(args.file)

config = Config.load()

client = HandyClient(config.api_secret)

logger.info('Getting Handy status')
data = client.status()
if data['mode'] != 1:
    client.set_mode(1)
logger.info('Connected to Handy')

logger.info('Uploading script')
client.upload_script(script)

syncer = TimeSyncer()
syncer.update_with_file(config.time_sync_file, client)

player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, osc=True)
hplayer = HandyPlayer(
    client=client,
    syncer=syncer,
)
player.play(args.file)
hplayer.attach_to(player)

# @player.event_callback('playback-restart')
def file_restart(event):
    time_ms = get_playback_time_ms(player)
    assert time_ms is not None
    hplayer.sync_play(time_ms)

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
        time_ms = get_playback_time_ms(player)
        if time_ms is not None:
            hplayer.sync_play(time_ms)

player.observe_property('pause', video_pause_unpause)

#@player.event_callback('unpause')
def video_unpause(event):
    time_ms = get_playback_time_ms(player)
    assert time_ms is not None
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
except Exception:
    hplayer.sync_play(0, stopped=True)
