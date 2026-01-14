from __future__ import annotations
from datetime import UTC, datetime
from . import db


class ImageRegistry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phash = db.Column(db.String(16), nullable=False, unique=True, index=True)
    whash = db.Column(db.String(16), nullable=False, index=True)
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


class ImageContainment(db.Model):
    __tablename__ = "image_containment"

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(
        db.Integer, db.ForeignKey("image_registry.id"), nullable=False, index=True
    )
    child_id = db.Column(
        db.Integer, db.ForeignKey("image_registry.id"), nullable=False, index=True
    )
    crop_box_json = db.Column(db.String(256), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    parent = db.relationship(
        "ImageRegistry", foreign_keys=[parent_id], backref="contains_images"
    )
    child = db.relationship(
        "ImageRegistry", foreign_keys=[child_id], backref="contained_in"
    )


class VoteHistory(db.Model):
    __table_args__ = (
        db.UniqueConstraint("image_id", "voter_id", name="uq_vote_history"),
    )
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("image_registry.id"), nullable=False)
    voter_id = db.Column(db.String(64), nullable=False)
    choice = db.Column(db.String(16), nullable=False)


class TinEyeResult(db.Model):
    __tablename__ = "tineye_result"

    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(
        db.Integer, db.ForeignKey("image_registry.id"), nullable=False, unique=True, index=True
    )

    total_matches = db.Column(db.Integer, nullable=False, default=0)
    filtered_match_count = db.Column(db.Integer, nullable=False, default=-1)
    earliest_date = db.Column(db.DateTime(timezone=True), nullable=True)
    on_shame_list = db.Column(db.Boolean, nullable=False, default=False)
    matches_json = db.Column(db.Text, nullable=False)

    searched_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    image = db.relationship(
        "ImageRegistry", backref=db.backref("tineye_result", uselist=False)
    )


class GlobalConfig(db.Model):
    __tablename__ = "global_config"

    id = db.Column(db.Integer, primary_key=True)
    total_donated_cents = db.Column(db.Integer, nullable=False, default=0)
