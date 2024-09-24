import threading
from datetime import datetime
from typing import Dict, Generator, List, Union

from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.scrapers.annatar import Annatar
from program.scrapers.comet import Comet
from program.scrapers.jackett import Jackett
from program.scrapers.knightcrawler import Knightcrawler
from program.scrapers.mediafusion import Mediafusion
from program.scrapers.orionoid import Orionoid
from program.scrapers.prowlarr import Prowlarr
from program.scrapers.shared import _parse_results
from program.scrapers.torbox import TorBoxScraper
from program.scrapers.torrentio import Torrentio
from program.scrapers.zilean import Zilean
from program.settings.manager import settings_manager
from utils.logger import logger


class Scraping:
    def __init__(self):
        self.key = "scraping"
        self.initialized = False
        self.settings = settings_manager.settings.scraping
        self.services = {
            Annatar: Annatar(),
            Torrentio: Torrentio(),
            Knightcrawler: Knightcrawler(),
            Orionoid: Orionoid(),
            Jackett: Jackett(),
            TorBoxScraper: TorBoxScraper(),
            Mediafusion: Mediafusion(),
            Prowlarr: Prowlarr(),
            Zilean: Zilean(),
            Comet: Comet()
        }
        self.initialized = self.validate()
        if not self.initialized:
            return

    def validate(self):
        return any(service.initialized for service in self.services.values())

    def yield_incomplete_children(self, item: MediaItem) -> Union[List[Season], List[Episode]]:
        if isinstance(item, Season):
            return [e for e in item.episodes if e.state != States.Completed and e.is_released and self.should_submit(e)]
        if isinstance(item, Show):
            return [s for s in item.seasons if s.state != States.Completed and s.is_released and self.should_submit(s)]
        return None

    def partial_state(self, item: MediaItem) -> bool:
        if item.last_state != States.PartiallyCompleted or self.can_we_scrape(item):
            return False
        if isinstance(item, Show):
            sres = [s for s in item.seasons if s.state != States.Completed and s.is_released and self.should_submit(s)]
            res = []
            for s in sres:
                if all(episode.is_released and episode.state != States.Completed for episode in s.episodes):
                    res.append(s)
                else:
                    res = res + [e for e in s.episodes if e.is_released  and e.state != States.Completed]
            return res
        if isinstance(item, Season):
            return [e for e in item.episodes if e.is_released]
        return item

    def run(self, item: Union[Show, Season, Episode, Movie]) -> Generator[Union[Show, Season, Episode, Movie], None, None]:
        """Scrape an item."""
        if self.can_we_scrape(item):
            sorted_streams = self.scrape(item)
            for stream in sorted_streams.values():
                if stream not in item.streams:
                    item.streams.append(stream)
            item.set("scraped_at", datetime.now())
            item.set("scraped_times", item.scraped_times + 1)

        if not item.get("streams", []):
            logger.log("NOT_FOUND", f"Scraping returned no good results for {item.log_string}")

        yield item

    def scrape(self, item: MediaItem, log = True) -> Dict[str, Stream]:
        """Scrape an item."""
        threads: List[threading.Thread] = []
        results: Dict[str, str] = {}
        total_results = 0
        results_lock = threading.RLock()

        def run_service(service, item,):
            nonlocal total_results
            service_results = service.run(item)
            with results_lock:
                results.update(service_results)
                total_results += len(service_results)

        for service_name, service in self.services.items():
            if service.initialized:
                thread = threading.Thread(target=run_service, args=(service, item), name=service_name.__name__)
                threads.append(thread)
                thread.start()

        for thread in threads:
            thread.join()

        if total_results != len(results):
            logger.debug(f"Scraped {item.log_string} with {total_results} results, removed {total_results - len(results)} duplicate hashes")

        sorted_streams: Dict[str, Stream] = _parse_results(item, results, log)

        if sorted_streams and (log and settings_manager.settings.debug):
            item_type = item.type.title()
            top_results = list(sorted_streams.values())[:10]
            for sorted_tor in top_results:
                if isinstance(item, (Movie, Show)):
                    logger.debug(f"[{item_type}] Parsed '{sorted_tor.parsed_title}' with rank {sorted_tor.rank} ({sorted_tor.infohash}): '{sorted_tor.raw_title}'")
                if isinstance(item, Season):
                    logger.debug(f"[{item_type} {item.number}] Parsed '{sorted_tor.parsed_title}' with rank {sorted_tor.rank} ({sorted_tor.infohash}): '{sorted_tor.raw_title}'")
                elif isinstance(item, Episode):
                    logger.debug(f"[{item_type} {item.parent.number}:{item.number}] Parsed '{sorted_tor.parsed_title}' with rank {sorted_tor.rank} ({sorted_tor.infohash}): '{sorted_tor.raw_title}'")
        return sorted_streams

    @classmethod
    def can_we_scrape(cls, item: MediaItem) -> bool:
        """Check if we can scrape an item."""
        if not item.is_released:
            logger.debug(f"Cannot scrape {item.log_string}: Item is not released")
            return False
        if not cls.should_submit(item):
            logger.debug(f"Cannot scrape {item.log_string}: Item has been scraped recently, backing off")
            return False
        return True

    @staticmethod
    def should_submit(item: MediaItem) -> bool:
        """Check if an item should be submitted for scraping."""
        settings = settings_manager.settings.scraping
        scrape_time = 5 * 60  # 5 minutes by default

        if item.scraped_times >= 2 and item.scraped_times <= 5:
            scrape_time = settings.after_2 * 60 * 60
        elif item.scraped_times > 5 and item.scraped_times <= 10:
            scrape_time = settings.after_5 * 60 * 60
        elif item.scraped_times > 10:
            scrape_time = settings.after_10 * 60 * 60

        return (
            not item.scraped_at
            or (datetime.now() - item.scraped_at).total_seconds() > scrape_time
        )