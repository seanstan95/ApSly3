from typing import Optional, Dict
import asyncio
import multiprocessing
import traceback

from CommonClient import logger, server_loop, gui_enabled, get_base_parser
from BaseClasses import ItemClassification
import Utils

from .data import Items, Locations
from .data.Constants import EPISODES, CHALLENGES, REQUIREMENTS
from .Sly3Interface import Sly3Interface, Sly3Episode, PowerUps
from .Sly3Callbacks import init, update

# Load Universal Tracker
tracker_loaded: bool = False
try:
  from worlds.tracker.TrackerClient import (
    TrackerCommandProcessor as ClientCommandProcessor,
    TrackerGameContext as CommonContext,
    UT_VERSION
  )

  tracker_loaded = True
except ImportError:
  from CommonClient import ClientCommandProcessor, CommonContext

class Sly3CommandProcessor(ClientCommandProcessor): # type: ignore[misc]
  def _cmd_deathlink(self):
    """Toggle deathlink from client. Overrides default setting."""
    if isinstance(self.ctx, Sly3Context):
      self.ctx.death_link_enabled = not self.ctx.death_link_enabled
      Utils.async_start(
        self.ctx.update_death_link(
          self.ctx.death_link_enabled
        ),
        name="Update Deathlink"
      )
      message = f"Deathlink {'enabled' if self.ctx.death_link_enabled else 'disabled'}"
      logger.info(message)
      self.ctx.notification(message)

  def _cmd_menu(self):
    """Reload to the episode menu"""
    if isinstance(self.ctx, Sly3Context):
      self.ctx.game_interface.to_episode_menu()

  def _cmd_reload(self):
    """Reload (in case you're stuck)"""
    if isinstance(self.ctx, Sly3Context):
      self.ctx.game_interface._reload()

  # def _cmd_coins(self, amount: str):
  #   """Add coins to game."""
  #   if isinstance(self.ctx, Sly3Context):
  #     self.ctx.game_interface.add_coins(int(amount))

  # def _cmd_notification(self, text: str):
  #   """Send a notification to the game."""
  #   if isinstance(self.ctx, Sly3Context):
  #     self.ctx.notification(text)

class Sly3Context(CommonContext): # type: ignore[misc]
  command_processor = Sly3CommandProcessor
  game_interface: Sly3Interface
  game = "Sly 3: Honor Among Thieves"
  items_handling = 0b111
  pcsx2_sync_task: Optional[asyncio.Task] = None
  is_connected_to_game: bool = False
  is_connected_to_server: bool = False
  slot_data: Optional[Dict[str, Utils.Any]] = None
  last_error_message: Optional[str] = None
  notification_queue: list[str] = []
  notification_timestamp: float = 0
  showing_notification: bool = False
  deathlink_timestamp: float = 0
  death_link_enabled = False
  queued_deaths: int = 0

  # Game state
  is_loading: bool = False
  in_safehouse: bool = False
  in_hub: bool = False
  current_map: Optional[int] = None
  current_job: Optional[int] = None
  current_episode: Sly3Episode = Sly3Episode.Title_Screen

  # Items and checks
  inventory: Dict[int,int] = {l.code: 0 for l in Items.item_dict.values()}
  available_episodes: Dict[Sly3Episode,bool] = {e: False for e in Sly3Episode}
  thiefnet_items: Optional[list[str]] = None
  powerups: PowerUps = PowerUps()
  thiefnet_purchases: PowerUps = PowerUps()
  jobs_completed: list[bool] = [
    False for episode in EPISODES.values()
    for chapter in episode
    for _ in chapter
  ]
  challenges_completed: list[bool] = [
    False for episode in CHALLENGES.values()
    for chapter in episode
    for _ in chapter
  ]

  def __init__(self, server_address, password):
    super().__init__(server_address, password)
    self.version = [0,1,3]
    self.game_interface = Sly3Interface(logger)
    self.available_episodes[Sly3Episode.Title_Screen] = True
    self.logger = logger

  def on_deathlink(self, data: Utils.Dict[str, Utils.Any]) -> None:
    super().on_deathlink(data)
    if self.death_link_enabled:
      self.queued_deaths += 1
      cause = data.get("cause", "")
      if cause:
        self.notification(f"DeathLink: {cause}")
      else:
        self.notification(f"DeathLink: Received from {data['source']}")

  def make_gui(self):
    ui = super().make_gui()
    ui.base_title = f"Sly 3 Client v{'.'.join([str(i) for i in self.version])}"
    if tracker_loaded:
        ui.base_title += f" | Universal Tracker {UT_VERSION}"

    # AP version is added behind this automatically
    ui.base_title += " | Archipelago"

    # Making the out of logic tab
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView
    from kivy.metrics import dp

    def make_left_label(**kwargs):
      lbl = Label(**kwargs)
      lbl.bind(width=lambda instance, value: setattr(instance, 'text_size', (value, None)))  # type: ignore
      lbl.bind(texture_size=lambda instance, value: setattr(instance, 'height', value[1]))  # type: ignore
      return lbl

    container = BoxLayout(
      orientation='vertical',
      padding=dp(10),
      spacing=dp(8),
      size_hint_y=None,
    )
    container.bind(minimum_height=container.setter('height')) # type: ignore

    container.add_widget(Label(
      text="Out of logic locations and their required progression items",
      font_size=dp(16),
      bold=True,
      size_hint_y=None,
      height=dp(40),
      halign="center",
      valign="middle",
    ))

    container.add_widget(make_left_label(
      text="Jobs",
      font_size=dp(14),
      bold=True,
      size_hint_y=None,
      height=dp(30),
      halign="left",
      valign="bottom",
    ))
    self.jobs_label = make_left_label(
      size_hint_y=None,
      halign="left",
      valign="top",
    )
    container.add_widget(self.jobs_label)

    container.add_widget(make_left_label(
      text="Master Thief Challenges",
      font_size=dp(14),
      bold=True,
      size_hint_y=None,
      height=dp(30),
      halign="left",
      valign="bottom",
    ))
    self.challenges_label = make_left_label(
      size_hint_y=None,
      halign="left",
      valign="top",
    )
    container.add_widget(self.challenges_label)

    scroll = ScrollView(size_hint=(1, 1))
    scroll.add_widget(container)
    self.out_of_logic_tab = scroll

    class Manager(ui):
      def build(self):
        super().build()
        self.add_client_tab("Out-of-Logic", self.ctx.out_of_logic_tab)
        return self.container

    return Manager

  def update_gui(self):
    received_items = [Items.from_id(i.item) for i in self.items_received]
    progression_items = [i.name for i in received_items if i.classification == ItemClassification.progression]

    section_requirements = {
      episode_name: [
        list(set(sum([
          sum(
            ep_reqs,
            []
          )
          for ep_reqs
          in episode[:i-1]
        ], [])))+[episode_name]
        for i in range(1,5)
      ]
      for episode_name, episode in REQUIREMENTS["Jobs"].items()
    }

    # Jobs
    jobs = [
      f"{ep_name} - {job}"
      for ep_name, ep in EPISODES.items() for chapter in ep for job in chapter
    ]
    job_requirements = [
      [r for r in reqs+section_requirements[ep_name][chapter_idx] if r not in progression_items]
      for ep_name, ep in REQUIREMENTS["Jobs"].items()
      for chapter_idx, chapter in enumerate(ep) for reqs in chapter
    ]
    job_pairs = zip(jobs,job_requirements)

    self.jobs_label.text = "\n".join([
      f"{job}:    {', '.join(reqs)}"
      for job, reqs in job_pairs
      if reqs != []
    ])

    # Challenges
    challenges = [
      f"{ep_name} - {challenge}"
      for ep_name, ep in CHALLENGES.items() for chapter in ep for challenge in chapter
    ]
    challenge_requirements = [
      sorted([r for r in list(set(reqs+section_requirements[ep_name][chapter_idx])) if r not in progression_items])
      for ep_name, ep in REQUIREMENTS["Challenges"].items()
      for chapter_idx, chapter in enumerate(ep) for reqs in chapter
    ]
    challenge_pairs = zip(challenges,challenge_requirements)

    self.challenges_label.text = "\n".join([
      f"{challenge}:    {', '.join(reqs)}"
      for challenge, reqs in challenge_pairs
      if reqs != []
    ])

  async def server_auth(self, password_requested: bool = False) -> None:
    if password_requested and not self.password:
      await super(Sly3Context, self).server_auth(password_requested)
    await self.get_username()
    await self.send_connect()

  def on_package(self, cmd: str, args: dict):
    super().on_package(cmd, args)
    if cmd == "Connected":
      self.slot_data = args["slot_data"]

      if self.version[:2] != args["slot_data"]["world_version"][:2]:
        raise Exception(f"World generation version and client version don't match up. The world was generated with version {args["slot_data"]["world_version"]}, but the client is version {self.version}")

      # thiefnet_n = args["slot_data"]["thiefnet_locations"]
      skipped_indices = set([28,36,37,39,40,42,43])
      total_length = 37+len(skipped_indices)
      val_iter = iter([
        Locations.location_dict[f"ThiefNet {i+1:02}"].code in self.checked_locations
        for i in range(37)
      ])
      self.thiefnet_purchases = PowerUps(*[False]*4+[
        False if i in skipped_indices else next(val_iter)
        for i in range(total_length)
      ])

      # Set death link tag if it was requested in options
      if "death_link" in args["slot_data"]:
        self.death_link_enabled = bool(args["slot_data"]["death_link"])
        Utils.async_start(self.update_death_link(
          bool(args["slot_data"]["death_link"])
        ))

      Utils.async_start(self.send_msgs([{
        "cmd": "LocationScouts",
        "locations": [
          Locations.location_dict[f"ThiefNet {i+1:02}"].code
          for i in range(args["slot_data"]["thiefnet_locations"])
        ]
      }]))
      self.update_gui()
    if cmd in ["RoomUpdate", "ReceivedItems"]:
      self.update_gui()

  def notification(self, text: str):
    self.logger.debug(f"Notification: {text}")
    self.notification_queue.append(text)

def update_connection_status(ctx: Sly3Context, status: bool):
  if ctx.is_connected_to_game == status:
    return

  if status:
    logger.info("Connected to Sly 3")
  else:
    logger.info("Unable to connect to the PCSX2 instance, attempting to reconnect...")

  ctx.is_connected_to_game = status

async def _handle_game_not_ready(ctx: Sly3Context):
  """If the game is not connected, this will attempt to retry connecting to the game."""
  if not ctx.exit_event.is_set():
    ctx.game_interface.connect_to_game()
  await asyncio.sleep(3)

async def _handle_game_ready(ctx: Sly3Context) -> None:
  current_map = ctx.game_interface.get_current_map()
  ctx.in_hub = ctx.game_interface.in_hub()
  ctx.current_job = ctx.game_interface.get_current_job()
  ctx.current_episode = ctx.game_interface.get_current_episode()

  ctx.game_interface.skip_cutscene()

  if ctx.game_interface.is_loading():
    ctx.is_loading = True
    await asyncio.sleep(0.5)
    return
  elif ctx.is_loading:
    ctx.is_loading = False
    await asyncio.sleep(1)

  connected_to_server = (ctx.server is not None) and (ctx.slot is not None)

  new_connection = ctx.is_connected_to_server != connected_to_server
  if ctx.current_map != current_map or new_connection:
    ctx.current_map = current_map
    ctx.is_connected_to_server = connected_to_server
    await init(ctx)

  await update(ctx)

  if ctx.server:
    ctx.last_error_message = None
    if not ctx.slot:
      await asyncio.sleep(1)
      return

    await asyncio.sleep(0.2)
  else:
    message = "Waiting for player to connect to server"
    if ctx.last_error_message is not message:
      logger.info("Waiting for player to connect to server")
      ctx.last_error_message = message
    await asyncio.sleep(1)

async def pcsx2_sync_task(ctx: Sly3Context):
  logger.info("Starting Sly 3 Connector, attempting to connect to emulator...")
  ctx.game_interface.connect_to_game()
  while not ctx.exit_event.is_set():
    try:
      is_connected = ctx.game_interface.get_connection_state()
      update_connection_status(ctx, is_connected)
      if is_connected:
        await _handle_game_ready(ctx)
      else:
        await _handle_game_not_ready(ctx)
    except ConnectionError:
      ctx.game_interface.disconnect_from_game()
    except Exception as e:
      if isinstance(e, RuntimeError):
        logger.error(str(e))
      else:
        logger.error(traceback.format_exc())
      await asyncio.sleep(3)
      continue

def launch_client():
  Utils.init_logging("Sly 3 Client")

  async def main(args):
    multiprocessing.freeze_support()
    logger.info("main")
    ctx = Sly3Context(args.connect, args.password)

    logger.info("Connecting to server...")
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="Server Loop")

    # Runs Universal Tracker's internal generator
    if tracker_loaded:
        ctx.run_generator()
        ctx.tags.remove("Tracker")

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    logger.info("Running game...")
    ctx.pcsx2_sync_task = asyncio.create_task(pcsx2_sync_task(ctx), name="PCSX2 Sync")

    await ctx.exit_event.wait()
    ctx.server_address = None

    await ctx.shutdown()

    if ctx.pcsx2_sync_task:
      await asyncio.sleep(3)
      await ctx.pcsx2_sync_task

  import colorama

  colorama.init()


  parser = get_base_parser()
  args, _ = parser.parse_known_args()

  asyncio.run(main(args))
  colorama.deinit()

if __name__ == "__main__":
  launch_client()
