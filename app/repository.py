"""Small deterministic local repository and actual GCP service implementations."""
import json
from pathlib import Path
import sqlite3
import threading

RECIPES = [
    {"id": "demo-recipe", "name": "Roasted tomato pasta", "minutes": 25,
     "description": "Weeknight pasta with slow-roasted cherry tomatoes and basil.",
     "ingredients": ["tomatoes", "pasta", "basil", "olive-oil", "garlic", "parmesan"],
     "steps": ["Roast tomatoes and garlic at 200°C for 15 minutes.", "Boil pasta until al dente.", "Toss with olive oil, tomatoes and basil. Finish with parmesan."],
     "color": "#b64e35"},
    {"id": "control-recipe", "name": "Garden chickpea salad", "minutes": 10,
     "description": "A crisp, bright salad that stays healthy during the demo incident.",
     "ingredients": ["chickpeas", "cucumber", "lemon"],
     "steps": ["Drain chickpeas and dice the cucumber.", "Toss with lemon juice and a pinch of salt."], "color": "#547a58"},
    {"id": "citrus-bowl", "name": "Morning citrus bowl", "minutes": 5,
     "description": "Sunny oranges, yogurt and a little honey.",
     "ingredients": ["orange", "yogurt", "honey"],
     "steps": ["Peel the orange and spoon into yogurt.", "Finish with a drizzle of honey."], "color": "#cf9b44"},
]


class LocalRepository:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))

    def mutate(self, key, update):
        """Atomically read/modify/write a small checkpoint under the local lock."""
        with self.lock:
            value, result = update(self.get(key, {}))
            self.set(key, value)
            return result

    def search(self, query=""):
        return [r for r in RECIPES if query.lower() in (r["name"] + " " + " ".join(r["ingredients"])).lower()]

    def recipe(self, recipe_id):
        return next((r for r in RECIPES if r["id"] == recipe_id), None)

    def ingredient(self, name):
        return {"name": name, "pantry": name in ("olive-oil", "garlic", "honey")}

    def ingredients(self, names):
        return [self.ingredient(name) for name in names]

    def image(self, recipe):
        # Generated SVG fixture, no external image license/account dependency.
        return svg(recipe)

    def close(self):
        self.db.close()


def svg(recipe):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 350">'
            f'<rect width="600" height="350" fill="{recipe["color"]}"/>'
            '<circle cx="300" cy="175" r="117" fill="#fff4df"/>'
            '<circle cx="300" cy="175" r="90" fill="#dfb479"/>'
            '<path d="M250 130Q300 190 350 130M245 185Q300 240 350 185" fill="none" stroke="#6e804e" stroke-width="20"/>'
            '</svg>').encode()


class CloudRepository:
    def __init__(self, settings):
        from google.cloud import firestore, storage
        self.client = firestore.Client(project=settings.project, database=settings.firestore_database)
        self.collection = self.client.collection(settings.prefix)
        self.bucket = storage.Client(project=settings.project).bucket(settings.bucket)
        self.prefix = settings.prefix

    def get(self, key, default=None):
        snap = self.collection.document(key).get(timeout=3)
        return snap.to_dict().get("value", default) if snap.exists else default

    def set(self, key, value):
        self.collection.document(key).set({"value": value}, timeout=3)

    def mutate(self, key, update):
        """One Firestore transaction prevents concurrent map lost updates."""
        from google.cloud import firestore
        reference = self.collection.document(key)
        @firestore.transactional
        def apply(transaction):
            snapshot = reference.get(transaction=transaction, timeout=3)
            prior = snapshot.to_dict().get("value", {}) if snapshot.exists else {}
            value, result = update(prior)
            transaction.set(reference, {"value": value})
            return result
        return apply(self.client.transaction(max_attempts=3))

    def search(self, query=""):
        # Tiny bounded seeded catalog avoids indexing assumptions and unbounded scans.
        values = [s.to_dict()["value"] for s in self.collection.where(
            filter=__import__("google.cloud.firestore_v1.base_query", fromlist=["FieldFilter"]).FieldFilter(
                "kind", "==", "recipe")).limit(10).stream(timeout=3)]
        return [r for r in values if query.lower() in (r["name"] + " " + " ".join(r["ingredients"])).lower()]

    def recipe(self, recipe_id):
        return self.get("recipe-" + recipe_id)

    def ingredient(self, name):
        return self.get("ingredient-" + name, {"name": name, "pantry": False})

    def ingredients(self, names):
        refs = [self.collection.document("ingredient-" + name) for name in names]
        found = {s.id: s.to_dict()["value"] for s in self.client.get_all(refs, timeout=3) if s.exists}
        return [found.get("ingredient-" + name, {"name": name, "pantry": False}) for name in names]

    def image(self, recipe):
        return self.bucket.blob(self.prefix + "/fixtures/" + recipe["id"] + ".svg").download_as_bytes(timeout=3)

    def seed(self):
        for recipe in RECIPES:
            self.collection.document("recipe-" + recipe["id"]).set({"kind": "recipe", "value": recipe}, timeout=3)
            self.bucket.blob(self.prefix + "/fixtures/" + recipe["id"] + ".svg").upload_from_string(
                svg(recipe), content_type="image/svg+xml", timeout=3)
            for name in recipe["ingredients"]:
                self.set("ingredient-" + name, {"name": name, "pantry": name in ("olive-oil", "garlic", "honey")})

    def close(self):
        self.client.close()

    def cleanup(self):
        # This collection and object prefix are exclusive to this app, including reuse mode.
        for snapshot in self.collection.limit(256).stream(timeout=4):
            snapshot.reference.delete(timeout=4)
        for blob in self.bucket.list_blobs(prefix=self.prefix + "/fixtures/", max_results=10, timeout=4):
            blob.delete(timeout=4)
