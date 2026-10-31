"""Load inspectable prompt files once, without the production prompt store."""
from dataclasses import dataclass
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class PromptCatalog:
    system: str
    guardrails: str
    care: str
    search: str
    photo: str

    @classmethod
    def load(cls, directory=None):
        root = Path(directory) if directory else Path(__file__).resolve().parents[1] / "prompts" / "V1"
        if (root / "manifest.json").exists():
            from .release import load_bundle
            return load_bundle(root).catalog
        values = {name: (root / f"{name}.txt").read_text().strip()
                  for name in cls.__dataclass_fields__}
        if any(not text for text in values.values()):
            raise ValueError("Prompt files must not be empty")
        return cls(**values)

    def messages(self):
        return [{"role": "system", "content": self.system},
                {"role": "developer", "content": "\n\n".join((self.guardrails, self.care, self.search))}]

    def snapshot(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def hashes(self):
        return {name: hashlib.sha256(text.encode()).hexdigest()
                for name, text in self.snapshot().items()}
