#!/usr/bin/python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import io
import json
import os
import sys
import time
import typing

if typing.TYPE_CHECKING:
    from typing import Optional

import mpv
from mpv import ShutdownError
from PIL import Image, ImageDraw, ImageFont
import requests

import config

API_SECRET=config.API_SECRET
API_ENDPOINT="https://www.handyfeeling.com/api/handy/v2/"

time_sync_initial_offset = 0
time_sync_average_offset = 0


HEADERS = {
    'X-Connection-Key': API_SECRET
}

parser = argparse.ArgumentParser(description='Handy MPV sync Utility')
parser.add_argument('file', metavar='file', type=str,
                   help='The file to play')

# this code is actually really dumb, should refactor, an intern probably
# did this. I'm just copying the JS code from the site.

@dataclass
class TimeSyncInfo:
    last_saved: int = 0
    average_offset: float = 0
    initial_offset: int = 0

    @staticmethod
    def from_file(path: str) -> TimeSyncInfo:
        if not os.path.exists(path):
            return TimeSyncInfo()
        with open(path, 'r') as f:
            obj = json.load(f)
            return TimeSyncInfo(
                last_saved=obj['last_saved'],
                average_offset=obj['time_sync_average_offset'],
                initial_offset=obj['time_sync_initial_offset'],
            )

    def write_to(self, path: str):
        if not os.path.exists(path):
            fp = open(path, 'x')
            fp.close()
        with open(path, 'w') as f:
            json.dump({
                'last_saved': self.last_saved,
                'time_sync_average_offset': self.average_offset,
                'time_sync_initial_offset': self.initial_offset,
            }, f)

class TSIManager:

    def __init__(self):
        self.aggregate_offset: int = 0
        self.sync_count: int = 0

    def save_to(self, path: str):
        tsi = TimeSyncInfo(
                last_saved=time.time_ns(),
                average_offset=time_sync_average_offset,
                initial_offset=time_sync_initial_offset,
        )
        tsi.write_to(path)

    def get_server_time(self):
        time_now = int(time.time_ns() / 1000000)
        return int(time_now + time_sync_average_offset + time_sync_initial_offset)

    def update_server_time(self):
        global time_sync_initial_offset, \
                time_sync_average_offset

        send_time = int(time.time_ns() / 1000000) # don't ask
        r = requests.get(f'{API_ENDPOINT}servertime', headers=HEADERS)
        data = json.loads(r.text)
        server_time = data['serverTime']
        print(server_time)
        time_now = int(time.time_ns() / 1000000)
        print(time_now)
        rtd = time_now - send_time
        estimated_server_time_now = int(server_time + rtd / 2)

        # this part here, real dumb.
        if self.sync_count == 0:
            time_sync_initial_offset = estimated_server_time_now - time_now
            print(f'initial offset {time_sync_initial_offset} ms')
        else:
            offset = estimated_server_time_now - time_now - time_sync_initial_offset
            self.aggregate_offset += offset
            time_sync_average_offset = self.aggregate_offset / self.sync_count

        self.sync_count += 1
        if self.sync_count < 30:
            self.update_server_time()
        else:
            print(f'we in sync, Average offset is: {int(time_sync_average_offset)} ms')
            return

manager = TSIManager()


def find_script(video_path):
    video_name = video_path.replace('.' + str.split(video_path, '.')[-1:][0], '')
    script_path = f'{video_name}.funscript'
    if (os.path.exists(script_path)):
        print(f'script found for video: {video_name}')
    return script_path

def upload_script(script):
    r = requests.post("https://tugbud.kaffesoft.com/cache", files={'file': open(script, 'rb')})
    data = json.loads(r.text)
    print(data)
    r = requests.put(f'{API_ENDPOINT}hssp/setup', json={'url': data['url']}, headers=HEADERS)
    data = json.loads(r.text)

print('Getting Handy Status')
r = requests.get(f'{API_ENDPOINT}status', headers=HEADERS)
data = json.loads(r.text)

if not data['mode']:
    print('Couldn\'t Sync with Handy, Exiting.')
    exit()

if data['mode'] != 1:
    r = requests.put(f'{API_ENDPOINT}/mode', json={"mode": 1}, headers=HEADERS)
    print(r.text)

print('Handy connected, Uploading script!')

args = parser.parse_args()
print(args)
script = find_script(args.file)
upload_script(script)


tsi = TimeSyncInfo.from_file(config.TIME_SYNC_FILE)

if  time.time_ns() - tsi.last_saved < 3600000000000:
    time_sync_average_offset = tsi.average_offset
    time_sync_initial_offset = tsi.initial_offset
else :
    manager.update_server_time()
    manager.save_to(config.TIME_SYNC_FILE)

player = mpv.MPV(input_default_bindings=True, input_vo_keyboard=True, osc=True)
player.play(args.file)
# font = ImageFont.truetype('DejaVuSans.ttf', 40)


# overlay = player.create_image_overlay()
# img = Image.new('RGBA', (400, 150),  (255, 255, 255, 0))
# d = ImageDraw.Draw(img)

sync = 0

def sync_play(time=0, play='true'):
    payload = {
        'estimatedServerTime': manager.get_server_time(),
        'startTime': time
    }

    if play == 'false':
        r = requests.put(f'{API_ENDPOINT}hssp/stop', headers=HEADERS)
        return

    r = requests.put(f'{API_ENDPOINT}hssp/play', json=payload, headers=HEADERS)
    print(r.text)

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
    sync_play(time_ms, 'false')

# @player.on_key_press('q')
def my_q_binding(key_state, key_name, key_char):
    global player
    sync_play(0, 'false')
    player.command("quit")
    del player
    os._exit(-1)

# @player.on_key_press('down')
def my_down_binding(key_state, key_name, key_char):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    print(time_ms)
    sync_play(time_ms, 'true')


player.register_key_binding("up", my_up_binding)
player.register_key_binding("q", my_q_binding)
player.register_key_binding("down", my_down_binding)

# @player.event_callback('playback-restart')
def file_restart(event):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    print(time_ms)
    sync_play(time_ms)
    print(f'Now playing at {time_ms}s')

# @player.event_callback('shutdown')
def callback_shutdown(event):
    sync_play(0, 'false')
    player.command("quit")
    sys.exit()

#@player.event_callback('pause')
def video_pause(event):
    sync_play(0, 'false')

def video_pause_unpause(property_name, new_value):
    paused = new_value
    if paused:
        sync_play(0, 'false')
    else:
        value = get_playback_time(player)
        if value is not None:
            time_ms = int(value * 1000)
            sync_play(time_ms, 'true')

player.observe_property('pause', video_pause_unpause)

#@player.event_callback('unpause')
def video_unpause(event):
    value = get_playback_time(player)
    assert value is not None
    time_ms = int(value * 1000)
    sync_play(time_ms, 'true')


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
except ShutdownError as e:
    sync_play(0, 'false')
    del player
    exit()
