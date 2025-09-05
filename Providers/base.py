# providers/base.py
from abc import ABC, abstractmethod
from typing import List, Dict

class Provider(ABC):
    @abstractmethod
    def pull_blocks(self) -> List[Dict]:
        """Return a list of upcoming blocks as dicts with keys time, label, category."""
        ...

    @abstractmethod
    def push_event(self, block: Dict) -> None:
        """(Optional) Push an event/reminder for a block to the service."""
        ...
