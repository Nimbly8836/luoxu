import argparse
import asyncio
import inspect
import logging

from .util import create_client, load_config


async def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', default='config.toml',
                      help='config file path')
  args = parser.parse_args()
  config = load_config(args.config)
  tg_config = config['telegram']
  client = create_client(tg_config)
  started = client.start(tg_config['account'])
  if inspect.isawaitable(started):
    await started
  dialogs = await client.get_dialogs()
  for d in reversed(dialogs):
    print(f'{d.entity.id:10} {d.name}')

if __name__ == '__main__':
  from .lib.nicelogger import enable_pretty_logging
  enable_pretty_logging(logging.INFO)
  asyncio.run(main())
