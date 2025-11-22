from __future__ import annotations

from datetime import UTC, datetime

from . import db


class ImageConsensus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, index=True)
    vote_real = db.Column(db.Integer, default=0)
    vote_edited = db.Column(db.Integer, default=0)
    vote_ai = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, int | str]:
        total_votes = (self.vote_real or 0) + (self.vote_edited or 0) + (self.vote_ai or 0)
        return {
            "phash": self.phash,
            "vote_real": self.vote_real,
            "vote_edited": self.vote_edited,
            "vote_ai": self.vote_ai,
            "total_votes": total_votes,
        }


class VoteHistory(db.Model):
    __table_args__ = (
        db.UniqueConstraint("phash", "voter_id", name="uq_vote_history_phash_voter"),
    )

    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, index=True)
    voter_id = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))


class ImageSource(db.Model):
    __table_args__ = (
        db.UniqueConstraint("phash", "url", name="uq_image_source_phash_url"),
    )

    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(UTC))
