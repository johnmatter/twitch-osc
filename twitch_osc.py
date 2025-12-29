#!/usr/bin/env python3
"""
Twitch-to-OSC Bridge

Connects to Twitch IRC (anonymous, read-only) and forwards chat messages
to local applications via OSC (Open Sound Control).

Usage:
    python twitch_osc.py
    python twitch_osc.py --config path/to/config.yaml
"""

import argparse
import asyncio
import os
import random
import time
from pathlib import Path
from typing import Optional

import irctokens
import yaml
from pythonosc import udp_client


# CTCP delimiter (ASCII 0x01)
CTCP_DELIM = '\x01'


class Config:
    """Configuration loaded from YAML file."""

    def __init__(self, config_path: Path):
        with open(config_path) as f:
            data = yaml.safe_load(f)

        self.channels: list[str] = data.get('channels', [])
        if not self.channels:
            raise ValueError("No channels specified in config")

        osc_config = data.get('osc', {})
        self.osc_host: str = osc_config.get('host', '127.0.0.1')
        self.osc_port: int = osc_config.get('port', 9000)
        self.osc_address: str = osc_config.get('address', '/twitch/chat')


def parse_ctcp(message: str) -> tuple[str, str]:
    """
    Parse CTCP message, extracting type and content.

    CTCP messages are wrapped in \\x01 delimiters and contain a command
    followed by optional parameters. Common types:
    - ACTION: /me messages (e.g., "\\x01ACTION waves hello\\x01")
    - VERSION, PING, TIME: Client queries (rarely seen in Twitch chat)

    Returns:
        tuple: (ctcp_type, content)
        - Normal message: ("", "Hello everyone!")
        - ACTION: ("ACTION", "waves hello")
        - VERSION query: ("VERSION", "")
        - Other CTCP: (type, params)
    """
    if message.startswith(CTCP_DELIM) and message.endswith(CTCP_DELIM):
        # Strip CTCP delimiters
        inner = message[1:-1]

        # Split into command and params
        if ' ' in inner:
            ctcp_type, content = inner.split(' ', 1)
        else:
            ctcp_type, content = inner, ""

        return ctcp_type.upper(), content

    return "", message


def parse_badges(badges_str: str) -> str:
    """
    Parse Twitch badges tag into a readable format.

    Input: "broadcaster/1,subscriber/24"
    Output: "broadcaster,subscriber/24"
    """
    if not badges_str:
        return ""

    badges = []
    for badge in badges_str.split(','):
        if '/' in badge:
            name, version = badge.split('/', 1)
            # Include version for subscriber (months) and bits
            if name in ('subscriber', 'bits', 'sub-gifter'):
                badges.append(f"{name}/{version}")
            else:
                badges.append(name)
        else:
            badges.append(badge)
    return ','.join(badges)


class TwitchOSCBridge:
    """Bridges Twitch chat to OSC messages."""

    def __init__(self, config: Config):
        self.config = config
        self.osc_client = udp_client.SimpleUDPClient(
            config.osc_host,
            config.osc_port
        )
        self._running = False
        self._debug = os.environ.get('TWITCH_DEBUG', '').lower() in ('1', 'true', 'yes')

    async def start(self):
        """Start the bridge, connecting to all configured channels."""
        self._running = True

        print(f"Starting Twitch-to-OSC bridge")
        print(f"OSC output: {self.config.osc_host}:{self.config.osc_port}")
        print(f"OSC address: {self.config.osc_address}")
        print(f"Channels: {', '.join(self.config.channels)}")
        print()

        # Create a task for each channel
        tasks = [
            asyncio.create_task(self._connect_channel(channel))
            for channel in self.config.channels
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def _connect_channel(self, channel: str):
        """Connect to a single Twitch channel via IRC."""
        # Generate anonymous username
        username = f"justinfan{random.randint(10000, 99999)}"

        reader: Optional[asyncio.StreamReader] = None
        writer: Optional[asyncio.StreamWriter] = None

        try:
            # Connect to Twitch IRC
            reader, writer = await asyncio.open_connection(
                'irc.chat.twitch.tv', 6667
            )

            # Request Twitch capabilities for rich metadata
            # tags: user color, badges, display-name, emotes, etc.
            # commands: enables /me and Twitch-specific messages
            cap_req = "CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"
            writer.write(cap_req.encode())
            await writer.drain()

            # Authenticate (anonymous)
            auth_msg = f"PASS anonymous\r\nNICK {username}\r\n"
            writer.write(auth_msg.encode())
            await writer.drain()

            # Join channel
            writer.write(f"JOIN #{channel}\r\n".encode())
            await writer.drain()

            # Read messages using irctokens
            await self._read_messages(channel, reader, writer)

        except Exception as e:
            print(f"[{channel}] Connection error: {e}")
            raise
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    async def _read_messages(
        self,
        channel: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """Read and process IRC messages from a channel."""
        decoder = irctokens.StatefulDecoder()

        while self._running:
            try:
                data = await reader.read(4096)
                if not data:
                    break

                # Feed data to irctokens decoder
                lines = decoder.push(data)
                if lines is None:
                    # Connection closed
                    break

                for line in lines:
                    await self._handle_line(channel, line, writer)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[{channel}] Error: {e}")
                if self._debug:
                    import traceback
                    traceback.print_exc()
                await asyncio.sleep(1)

    async def _handle_line(
        self,
        channel: str,
        line: irctokens.Line,
        writer: asyncio.StreamWriter
    ):
        """Handle a single parsed IRC line."""
        if self._debug:
            print(f"[{channel}] {line.command}: {line.params}")

        # Respond to PING
        if line.command == "PING":
            pong = irctokens.build("PONG", [line.params[0] if line.params else ""])
            writer.write(pong.format().encode() + b"\r\n")
            await writer.drain()
            return

        # Handle PRIVMSG (chat messages)
        if line.command == "PRIVMSG" and len(line.params) >= 2:
            self._handle_privmsg(channel, line)

    def _handle_privmsg(self, channel: str, line: irctokens.Line):
        """Handle a PRIVMSG (chat message)."""
        msg_channel = line.params[0].lstrip('#')
        raw_message = line.params[1]

        # Parse CTCP (handles /me ACTION and other CTCP types)
        ctcp_type, message = parse_ctcp(raw_message)

        # Extract user info from hostmask
        nickname = line.hostmask.nickname if line.hostmask else "unknown"

        # Extract rich metadata from Twitch tags
        tags = line.tags or {}

        # display-name is the properly capitalized username
        display_name = tags.get('display-name', nickname)

        # User's chosen chat color (hex, e.g., "#FF0000")
        color = tags.get('color', '')

        # Parse badges
        badges = parse_badges(tags.get('badges', ''))

        # Timestamp
        timestamp = int(time.time())

        # Send OSC message
        # Format: [channel, username, message, timestamp, ctcp_type, color, badges]
        self.osc_client.send_message(
            self.config.osc_address,
            [
                msg_channel,           # string: channel name
                display_name,          # string: username (display-name)
                message,               # string: message content (CTCP content extracted)
                timestamp,             # int: unix timestamp
                ctcp_type,             # string: CTCP type ("ACTION", "VERSION", "" for normal)
                color,                 # string: hex color or empty
                badges,                # string: comma-separated badges or empty
            ]
        )

        if self._debug:
            ctcp_marker = f" [{ctcp_type}]" if ctcp_type else ""
            print(f"[{channel}] {display_name}{ctcp_marker}: {message[:50]}")

    def stop(self):
        """Stop the bridge."""
        self._running = False


def find_config() -> Path:
    """Find config file, checking current directory and ~/.config."""
    # Check current directory
    local_config = Path('config.yaml')
    if local_config.exists():
        return local_config

    # Check ~/.config/twitch-osc/
    user_config = Path.home() / '.config' / 'twitch-osc' / 'config.yaml'
    if user_config.exists():
        return user_config

    raise FileNotFoundError(
        "No config.yaml found. Copy config.example.yaml to config.yaml and edit it."
    )


async def main():
    parser = argparse.ArgumentParser(
        description='Bridge Twitch chat to OSC messages'
    )
    parser.add_argument(
        '--config', '-c',
        type=Path,
        help='Path to config file (default: config.yaml or ~/.config/twitch-osc/config.yaml)'
    )
    args = parser.parse_args()

    # Find config
    try:
        config_path = args.config if args.config else find_config()
        config = Config(config_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Config error: {e}")
        return 1

    # Run bridge
    bridge = TwitchOSCBridge(config)

    try:
        await bridge.start()
    except KeyboardInterrupt:
        print("\nStopping...")
        bridge.stop()

    return 0


if __name__ == '__main__':
    exit(asyncio.run(main()))
