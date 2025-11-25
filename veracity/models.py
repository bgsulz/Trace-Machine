from __future__ import annotations
from datetime import UTC, datetime
from . import db


class ImageRegistry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, unique=True, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    consensus = db.relationship("ImageConsensus", backref="image", uselist=False)
    facts = db.relationship("ProvenanceFact", backref="image")
    sources = db.relationship("ImageSource", backref="image")


class ImageConsensus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(
        db.Integer, db.ForeignKey("image_registry.id"), nullable=False, unique=True
    )

    vote_real = db.Column(db.Integer, default=0)
    vote_edited = db.Column(db.Integer, default=0)
    vote_ai = db.Column(db.Integer, default=0)


class ProvenanceFact(db.Model):
    __table_args__ = (
        db.UniqueConstraint("image_id", "analyzer", "data", name="uq_fact"),
    )
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("image_registry.id"), nullable=False)
    analyzer = db.Column(db.String(50), nullable=False)
    data = db.Column(db.Text, nullable=False)  # Store simple strings or JSON strings


class ImageSource(db.Model):
    __table_args__ = (db.UniqueConstraint("image_id", "url", name="uq_source"),)
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("image_registry.id"), nullable=False)
    url = db.Column(db.Text, nullable=False)


class VoteHistory(db.Model):
    __table_args__ = (
        db.UniqueConstraint("image_id", "voter_id", name="uq_vote_history"),
    )
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("image_registry.id"), nullable=False)
    voter_id = db.Column(db.String(64), nullable=False)
