from __future__ import annotations

from datetime import datetime

from . import db


class ImageConsensus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, index=True)
    vote_real = db.Column(db.Integer, default=0)
    vote_ai = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict[str, int | str]:
        total_votes = (self.vote_real or 0) + (self.vote_ai or 0)
        return {
            "phash": self.phash,
            "vote_real": self.vote_real,
            "vote_ai": self.vote_ai,
            "total_votes": total_votes,
        }
