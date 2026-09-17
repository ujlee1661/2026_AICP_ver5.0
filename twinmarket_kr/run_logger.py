from __future__ import annotations

import csv
import json
import os
import shutil
import threading
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import config


LINEAGE_FIELDS = (
    "source_ltb_id",
    "source_stb_id",
    "analysis_id",
    "decision_id",
    "fill_id",
    "post_fill_ltb_id",
)


def _lineage_values(*sources: Mapping[str, Any] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for source in sources:
        if not source:
            continue
        for field in LINEAGE_FIELDS:
            raw = source.get(field)
            if raw is None or raw == "":
                continue
            value = str(raw)
            prior = values.get(field)
            if prior is not None and prior != value:
                raise ValueError(
                    f"logger lineage conflict for {field}: "
                    f"{prior!r} != {value!r}"
                )
            values[field] = value
    return values


class SimulationLogger:
    def __init__(
        self,
        *,
        root_dir: Path | str = config.LOG_DIR,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite_root: bool = False,
        append_existing: bool = False,
    ) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.run_id = run_id or f"simulation_{timestamp}_{os.getpid()}"
        root = Path(root_dir)
        if overwrite_root and root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        self.run_dir = root / self.run_id
        if self.run_dir.exists() and not append_existing:
            raise FileExistsError(
                f"Run log directory already exists: {self.run_dir}. "
                "Use the explicit resume path instead of overwriting it."
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "traces").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._agent_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "agent_id",
            *LINEAGE_FIELDS,
            "decision_date",
            "market_features_date",
            "news_start_date",
            "news_start_time",
            "news_max_date",
            "news_end_time",
            "execution_date",
            "information_mode",
            "news_depth",
            "visible_news_ids",
            "read_news_ids",
            "search_result_ids",
            "influential_news_ids",
            "unmapped_influential_news",
            "selected_news_count",
            "read_news_count",
            "search_read_count",
            "fake_exposed",
            "fake_visible",
            "fake_read",
            "fake_searched",
            "fake_influential",
            "fake_base_count",
            "fake_read_count",
            "fake_search_count",
            "fake_selected_count",
            "fake_public_ids",
            "fake_synthetic_ids",
            "fake_related_events",
            "depth2_search_keywords",
            "depth2_search_result_count",
            "depth2_view_change",
            "news_sentiment",
            "community_log_turn",
            "community_thinking",
            "action",
            "quantity",
            "submitted_order",
            "decision_attempts",
            "decision_retry_count",
            "decision_origin",
            "available_cash",
            "current_quantity",
            "max_buy_quantity",
            "max_sell_quantity",
            "selected_action_max_quantity",
            "one_share_reason",
            "deterministic_fallback_used",
            "belief_generation_attempts",
            "belief_summary",
            "view_change",
            "decision_reason",
            "risk_control",
            "order_corrections",
        ]
        self._orders_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "agent_id",
            *LINEAGE_FIELDS,
            "decision_date",
            "market_features_date",
            "news_start_date",
            "news_start_time",
            "news_max_date",
            "news_end_time",
            "execution_date",
            "information_mode",
            "stock_code",
            "action",
            "quantity",
            "reason",
            "decision_attempts",
            "decision_origin",
            "one_share_reason",
        ]
        self._fills_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "stock_code",
            "agent_id",
            *LINEAGE_FIELDS,
            "action",
            "quantity",
            "executed_price",
            "fee",
            "status",
        ]
        self._daily_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "stock_code",
            "submitted_orders",
            "announced_price",
            "close_price",
            "volume",
            "fill_count",
        ]
        self._community_posts_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "post_id",
            "source_ltb_id",
            "source_fill_id",
            "source_decision_id",
            "post_type",
            "title",
            "content",
        ]
        self._community_interactions_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "selected_post_ids",
            "post_id",
            "exposure_level",
            "selected",
            "is_best",
            "anonymous_code",
            "title",
            "post_type",
            "content",
            "body_sha256",
            "reaction",
            "author_profile",
            "profile_scope",
            "source_date",
            "delivery_date",
            "source_turn",
            "delivery_turn",
            "delivery_status",
            "replay",
            "provenance_id",
        ]
        self._community_selection_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "depth",
            "read_limit",
            "visible_post_count",
            "visible_post_ids",
            "visible_posts_json",
        ]
        self._community_best_csv_fields = [
            "run_id",
            "date",
            "turn",
            "rank",
            "post_id",
            "author_agent_id",
            "anonymous_code",
            "title",
            "post_type",
            "score",
            "like_count",
            "unlike_count",
            "content",
            "body_sha256",
            "author_profile_snapshot",
            "snapshot_turn",
            "snapshot_date",
            "audience_count",
            "scheduled_delivery_count",
            "actual_delivery_count",
            "self_excluded_count",
            "self_exclusion_policy",
            "delivery_status",
        ]
        self._community_logs_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "best_posts_count",
            "posts_read_count",
            "candidate_posts_seen_count",
            "community_thinking",
            "best_posts_json",
            "posts_read_json",
            "candidate_posts_seen_json",
        ]
        self._init_csv(self.run_dir / "agent_turns.csv", self._agent_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "submitted_orders.csv", self._orders_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "exchange_fills.csv", self._fills_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "daily_exchange_summary.csv", self._daily_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_posts.csv", self._community_posts_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_selection_inputs.csv", self._community_selection_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_interactions.csv", self._community_interactions_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_best_posts.csv", self._community_best_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_logs.csv", self._community_logs_csv_fields, append_existing=append_existing)
        self._community_provenance_ids = {
            str(row.get("provenance_id"))
            for row in self._read_csv_rows(self.run_dir / "community_interactions.csv")
            if row.get("provenance_id")
        }
        if not append_existing or not (self.run_dir / "run_metadata.json").exists():
            self.write_json("run_metadata.json", {"run_id": self.run_id, "created_at": timestamp, **(metadata or {})})

    def log_agent_turn(
        self,
        *,
        agent: dict[str, Any],
        turn: int,
        date: str,
        context: dict[str, Any],
        news_interpretation: dict[str, Any],
        belief: dict[str, Any],
        market_analysis: dict[str, Any],
        decision: dict[str, Any],
        order: dict[str, Any] | None,
        depth2_flow: dict[str, Any] | None = None,
        fake_news_audit: dict[str, Any] | None = None,
        lineage: Mapping[str, Any] | None = None,
    ) -> None:
        news_context = context.get("news_context") or {}
        lineage_ids = _lineage_values(
            context.get("lineage"),
            {
                "source_stb_id": belief.get("source_stb_id")
                or belief.get("stb_id"),
                "post_fill_ltb_id": belief.get("post_fill_ltb_id"),
            },
            decision,
            order,
            lineage,
        )
        event = {
            "run_id": self.run_id,
            "event": "agent_turn",
            "date": date,
            "turn": turn,
            "agent": self._compact_agent(agent),
            "context": context,
            "news_interpretation": news_interpretation,
            "belief": belief,
            "market_analysis": market_analysis,
            "decision": decision,
            "submitted_order": order,
        }
        if lineage_ids:
            event["lineage"] = lineage_ids
        if depth2_flow is not None:
            event["depth2_flow"] = depth2_flow
        if fake_news_audit is not None:
            event["fake_news_audit"] = fake_news_audit
        self.write_jsonl("agent_turns.jsonl", event)
        selected_news = news_interpretation.get("selected_news") or []
        influential_news_ids = [
            str(value) for value in news_context.get("influential_news_ids") or []
        ] or _news_reference_ids(selected_news)
        visible_news_ids = [str(value) for value in news_context.get("visible_news_ids") or []]
        read_news_ids = [str(value) for value in news_context.get("read_news_ids") or []]
        search_result_ids = [str(value) for value in news_context.get("search_result_ids") or []]
        step3 = (depth2_flow or {}).get("step3_search") or {}
        step4 = (depth2_flow or {}).get("step4_post_search_thinking") or {}
        self.append_csv(
            "agent_turns.csv",
            self._agent_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "subturn": context.get("subturn", "full"),
                "agent_id": agent.get("agent_id"),
                **lineage_ids,
                "decision_date": context.get("decision_date", date),
                "market_features_date": context.get("market_features_date", date),
                "news_start_date": context.get("news_start_date") or "",
                "news_start_time": context.get("news_start_time") or "",
                "news_max_date": context.get("news_max_date", date),
                "news_end_time": context.get("news_end_time") or "",
                "execution_date": context.get("execution_date", date),
                "information_mode": context.get("information_mode", "pre_close_cutoff"),
                "news_depth": news_context.get("news_depth"),
                "visible_news_ids": ", ".join(visible_news_ids),
                "read_news_ids": ", ".join(read_news_ids),
                "search_result_ids": ", ".join(search_result_ids),
                "influential_news_ids": ", ".join(influential_news_ids),
                "unmapped_influential_news": json.dumps(
                    news_interpretation.get("unmapped_selected_news") or [],
                    ensure_ascii=False,
                ),
                "selected_news_count": len(selected_news) if isinstance(selected_news, list) else 0,
                "read_news_count": len(news_context.get("read_contents") or []),
                "search_read_count": len(news_context.get("search_read_contents") or []),
                "fake_exposed": bool((fake_news_audit or {}).get("fake_exposed")),
                "fake_visible": bool((fake_news_audit or {}).get("fake_visible")),
                "fake_read": bool((fake_news_audit or {}).get("fake_read")),
                "fake_searched": bool((fake_news_audit or {}).get("fake_searched")),
                "fake_influential": bool((fake_news_audit or {}).get("fake_influential")),
                "fake_base_count": (fake_news_audit or {}).get("fake_base_count", 0),
                "fake_read_count": (fake_news_audit or {}).get("fake_read_count", 0),
                "fake_search_count": (fake_news_audit or {}).get("fake_search_count", 0),
                "fake_selected_count": (fake_news_audit or {}).get("fake_selected_count", 0),
                "fake_public_ids": ", ".join((fake_news_audit or {}).get("fake_public_ids") or []),
                "fake_synthetic_ids": ", ".join((fake_news_audit or {}).get("fake_synthetic_ids") or []),
                "fake_related_events": json.dumps(
                    (fake_news_audit or {}).get("fake_related_events") or [],
                    ensure_ascii=False,
                ),
                "depth2_search_keywords": ", ".join(step3.get("keywords") or []),
                "depth2_search_result_count": step3.get("result_count", ""),
                "depth2_view_change": step4.get("view_change", ""),
                "news_sentiment": news_interpretation.get("news_sentiment"),
                "community_log_turn": context.get("community_log_turn") or "",
                "community_thinking": context.get("community_thinking") or "",
                "action": decision.get("action"),
                "quantity": decision.get("quantity"),
                "submitted_order": bool(order),
                "decision_attempts": decision.get("decision_attempts"),
                "decision_retry_count": decision.get("decision_retry_count"),
                "decision_origin": decision.get("decision_origin"),
                "available_cash": decision.get("available_cash"),
                "current_quantity": decision.get("current_quantity"),
                "max_buy_quantity": decision.get("max_buy_quantity"),
                "max_sell_quantity": decision.get("max_sell_quantity"),
                "selected_action_max_quantity": decision.get("selected_action_max_quantity"),
                "one_share_reason": decision.get("one_share_reason"),
                "deterministic_fallback_used": decision.get("deterministic_fallback_used", False),
                "belief_generation_attempts": belief.get("generation_attempts"),
                "belief_summary": belief.get("belief_summary"),
                "view_change": belief.get("view_change"),
                "decision_reason": decision.get("reason"),
                "risk_control": decision.get("risk_control"),
                "order_corrections": ", ".join(decision.get("order_corrections") or []),
            },
        )
        if order:
            self.log_submitted_order(
                {**order, **lineage_ids},
                turn=turn,
                date=date,
            )

    def log_agent_error(self, *, agent: dict[str, Any], turn: int, date: str, error: BaseException) -> None:
        self.write_jsonl(
            "errors.jsonl",
            {
                "run_id": self.run_id,
                "event": "agent_error",
                "date": date,
                "turn": turn,
                "agent_id": agent.get("agent_id"),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def log_submitted_order(self, order: dict[str, Any], *, turn: int, date: str) -> None:
        self.append_csv(
            "submitted_orders.csv",
            self._orders_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "subturn": order.get("subturn", ""),
                "agent_id": order.get("user_id"),
                **_lineage_values(order),
                "decision_date": order.get("decision_date", date),
                "market_features_date": order.get("market_features_date", date),
                "news_start_date": order.get("news_start_date") or "",
                "news_start_time": order.get("news_start_time") or "",
                "news_max_date": order.get("news_max_date", date),
                "news_end_time": order.get("news_end_time") or "",
                "execution_date": order.get("execution_date", date),
                "information_mode": order.get("information_mode", ""),
                "stock_code": order.get("stock_code"),
                "action": order.get("direction") or order.get("action"),
                "quantity": order.get("quantity"),
                "reason": order.get("reason"),
                "decision_attempts": order.get("decision_attempts"),
                "decision_origin": order.get("decision_origin"),
                "one_share_reason": order.get("one_share_reason"),
            },
        )

    def log_daily_exchange(
        self,
        *,
        date: str,
        turn: int,
        orders: list[dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> None:
        self.write_jsonl(
            "daily_exchange.jsonl",
            {
                "run_id": self.run_id,
                "event": "daily_exchange",
                "date": date,
                "turn": turn,
                "submitted_orders": orders,
                "results": results,
            },
        )
        for stock_code, result in sorted(results.items()):
            transactions = result.get("transactions") or []
            orders_by_agent = {
                str(order.get("user_id") or order.get("agent_id")): order
                for order in orders
                if order.get("user_id") or order.get("agent_id")
            }
            self.append_csv(
                "daily_exchange_summary.csv",
                self._daily_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "subturn": _subturn_from_turn(turn),
                    "stock_code": stock_code,
                    "submitted_orders": len([order for order in orders if order.get("stock_code") == stock_code]),
                    "announced_price": result.get("announced_price"),
                    "close_price": result.get("close_price"),
                    "volume": result.get("volume"),
                    "fill_count": len(transactions),
                },
            )
            for tx in transactions:
                agent_id = tx.get("agent_id") or tx.get("user_id")
                lineage_ids = _lineage_values(
                    orders_by_agent.get(str(agent_id)),
                    tx,
                )
                self.append_csv(
                    "exchange_fills.csv",
                    self._fills_csv_fields,
                    {
                        "run_id": self.run_id,
                        "date": date,
                        "turn": turn,
                        "subturn": _subturn_from_turn(turn),
                        "stock_code": tx.get("stock_code", stock_code),
                        "agent_id": agent_id,
                        **lineage_ids,
                        "action": tx.get("action") or tx.get("direction"),
                        "quantity": tx.get("quantity") or tx.get("executed_quantity"),
                        "executed_price": tx.get("executed_price"),
                        "fee": tx.get("fee", 0),
                        "status": tx.get("status", "filled"),
                    },
                )

    def log_community_post(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        post: dict[str, Any],
    ) -> None:
        event = {
            "run_id": self.run_id,
            "event": "community_post",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "post": post,
        }
        self.write_jsonl("community_events.jsonl", event)
        self.append_csv(
            "community_posts.csv",
            self._community_posts_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "post_id": post.get("post_id"),
                "source_ltb_id": post.get("source_ltb_id"),
                "source_fill_id": post.get("source_fill_id")
                or post.get("fill_id"),
                "source_decision_id": post.get("source_decision_id")
                or post.get("decision_id"),
                "post_type": post.get("post_type"),
                "title": post.get("title"),
                "content": post.get("content"),
            },
        )

    def log_community_reading(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        selected_post_ids: list[int],
        posts_read: list[dict[str, Any]],
    ) -> None:
        event = {
            "run_id": self.run_id,
            "event": "community_reading",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "selected_post_ids": selected_post_ids,
            "posts_read": posts_read,
        }
        self.write_jsonl("community_events.jsonl", event)
        if not posts_read:
            return
        for post in posts_read:
            self._append_community_interaction_once(
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "selected_post_ids": json.dumps(selected_post_ids, ensure_ascii=False),
                    "post_id": post.get("post_id"),
                    "exposure_level": post.get("exposure_level", "full_body"),
                    "selected": True,
                    "is_best": bool(post.get("is_best", False)),
                    "anonymous_code": post.get("anonymous_code"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "content": post.get("content"),
                    "body_sha256": post.get("body_sha256"),
                    "reaction": post.get("reaction"),
                    "author_profile": json.dumps(post.get("author_profile"), ensure_ascii=False, default=str),
                    "profile_scope": post.get("profile_scope"),
                    "source_date": date,
                    "delivery_date": date,
                    "source_turn": turn,
                    "delivery_turn": turn,
                    "delivery_status": "read_pm",
                    "replay": False,
                    "provenance_id": (
                        f"community:{date}:t{turn}:post:{post.get('post_id')}:"
                        f"selected_full_body:{agent_id}"
                    ),
                },
            )

    def log_community_selection_input(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        depth: int,
        read_limit: int,
        visible_posts: list[dict[str, Any]],
        selected_post_ids: list[int] | None = None,
    ) -> None:
        selected_ids = {int(post_id) for post_id in (selected_post_ids or [])}
        compact_posts = [
            {
                "post_id": post.get("post_id"),
                "anonymous_code": post.get("anonymous_code"),
                "post_type": post.get("post_type"),
                "title": post.get("title"),
                "like_count": post.get("like_count"),
                "unlike_count": post.get("unlike_count"),
                "score": post.get("score"),
            }
            for post in visible_posts
        ]
        event = {
            "run_id": self.run_id,
            "event": "community_selection_input",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "depth": depth,
            "read_limit": read_limit,
            "selected_post_ids": sorted(selected_ids),
            "visible_posts": compact_posts,
        }
        self.write_jsonl("community_selection_inputs.jsonl", event)
        self.append_csv(
            "community_selection_inputs.csv",
            self._community_selection_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "depth": depth,
                "read_limit": read_limit,
                "visible_post_count": len(compact_posts),
                "visible_post_ids": json.dumps([post.get("post_id") for post in compact_posts], ensure_ascii=False),
                "visible_posts_json": json.dumps(compact_posts, ensure_ascii=False),
            },
        )
        for post in compact_posts:
            post_id = int(post["post_id"])
            self._append_community_interaction_once(
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "selected_post_ids": json.dumps(sorted(selected_ids), ensure_ascii=False),
                    "post_id": post_id,
                    "exposure_level": "title_only",
                    "selected": post_id in selected_ids,
                    "is_best": False,
                    "anonymous_code": post.get("anonymous_code"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "reaction": "",
                    "profile_scope": "candidate_minimal",
                    "source_date": date,
                    "delivery_date": date,
                    "source_turn": turn,
                    "delivery_turn": turn,
                    "delivery_status": "candidate_seen_pm",
                    "replay": False,
                    "provenance_id": (
                        f"community:{date}:t{turn}:post:{post_id}:"
                        f"title_only:{agent_id}"
                    ),
                },
            )

    def log_community_best_posts(self, *, turn: int, date: str, best_posts: list[dict[str, Any]]) -> None:
        self.write_jsonl(
            "community_events.jsonl",
            {
                "run_id": self.run_id,
                "event": "community_best_posts",
                "date": date,
                "turn": turn,
                "best_posts": best_posts,
            },
        )
        for rank, post in enumerate(best_posts, start=1):
            self.append_csv(
                "community_best_posts.csv",
                self._community_best_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "rank": rank,
                    "post_id": post.get("post_id"),
                    "author_agent_id": post.get("author_agent_id"),
                    "anonymous_code": post.get("anonymous_code"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "score": post.get("score"),
                    "like_count": post.get("like_count"),
                    "unlike_count": post.get("unlike_count"),
                    "content": post.get("content"),
                    "body_sha256": post.get("body_sha256"),
                    "author_profile_snapshot": json.dumps(
                        post.get("author_profile_snapshot"),
                        ensure_ascii=False,
                        default=str,
                    ),
                    "snapshot_turn": post.get("snapshot_turn"),
                    "snapshot_date": post.get("snapshot_date"),
                    "audience_count": post.get("audience_count"),
                    "scheduled_delivery_count": post.get("scheduled_delivery_count"),
                    "actual_delivery_count": post.get("actual_delivery_count"),
                    "self_excluded_count": post.get("self_excluded_count"),
                    "self_exclusion_policy": post.get("self_exclusion_policy"),
                    "delivery_status": post.get("delivery_status"),
                },
            )

    def log_community_delivery(
        self,
        *,
        agent_id: str,
        source_turn: int,
        delivery_turn: int,
        source_date: str,
        delivery_date: str,
        best_posts: list[dict[str, Any]],
        posts_read: list[dict[str, Any]],
    ) -> None:
        self.write_jsonl(
            "community_events.jsonl",
            {
                "run_id": self.run_id,
                "event": "community_delivery",
                "agent_id": agent_id,
                "source_turn": source_turn,
                "delivery_turn": delivery_turn,
                "source_date": source_date,
                "delivery_date": delivery_date,
                "best_posts": best_posts,
                "selected_posts_replayed": posts_read,
            },
        )
        relations = [
            ("best_full_body", True, False, post)
            for post in best_posts
        ]
        relations.extend(
            ("selected_full_body_replay", False, True, post)
            for post in posts_read
        )
        for relation, is_best, replay, post in relations:
            post_id = int(post["post_id"])
            self._append_community_interaction_once(
                {
                    "run_id": self.run_id,
                    "date": delivery_date,
                    "turn": delivery_turn,
                    "agent_id": agent_id,
                    "selected_post_ids": "",
                    "post_id": post_id,
                    "exposure_level": "full_body",
                    "selected": relation.startswith("selected_"),
                    "is_best": is_best,
                    "anonymous_code": post.get("anonymous_code"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "content": post.get("content"),
                    "body_sha256": post.get("body_sha256"),
                    "reaction": post.get("reaction") if replay else "",
                    "author_profile": json.dumps(
                        post.get("author_profile"),
                        ensure_ascii=False,
                        default=str,
                    ),
                    "profile_scope": post.get("profile_scope"),
                    "source_date": source_date,
                    "delivery_date": delivery_date,
                    "source_turn": source_turn,
                    "delivery_turn": delivery_turn,
                    "delivery_status": "delivered_am",
                    "replay": replay,
                    "provenance_id": (
                        f"community:{source_date}:t{source_turn}:post:{post_id}:"
                        f"{relation}:{agent_id}:delivered_t{delivery_turn}"
                    ),
                },
            )

    def log_community_log(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        best_posts: list[dict[str, Any]],
        posts_read: list[dict[str, Any]],
        candidate_posts_seen: list[dict[str, Any]] | None = None,
        community_thinking: str = "",
    ) -> None:
        candidate_posts = list(candidate_posts_seen or [])
        event = {
            "run_id": self.run_id,
            "event": "community_log_saved",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "best_posts": best_posts,
            "posts_read": posts_read,
            "candidate_posts_seen": candidate_posts,
            "community_thinking": community_thinking,
        }
        self.write_jsonl("community_events.jsonl", event)
        self.append_csv(
            "community_logs.csv",
            self._community_logs_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "best_posts_count": len(best_posts),
                "posts_read_count": len(posts_read),
                "candidate_posts_seen_count": len(candidate_posts),
                "community_thinking": community_thinking,
                "best_posts_json": json.dumps(best_posts, ensure_ascii=False),
                "posts_read_json": json.dumps(posts_read, ensure_ascii=False, default=str),
                "candidate_posts_seen_json": json.dumps(
                    candidate_posts,
                    ensure_ascii=False,
                    default=str,
                ),
            },
        )

    def write_json(self, filename: str, data: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def write_jsonl(self, filename: str, data: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")

    def append_csv(self, filename: str, fieldnames: list[str], row: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    def _append_community_interaction_once(self, row: dict[str, Any]) -> bool:
        provenance_id = str(row.get("provenance_id") or "")
        if not provenance_id:
            raise ValueError("community interaction requires a provenance_id")
        with self._lock:
            if provenance_id in self._community_provenance_ids:
                return False
            with (self.run_dir / "community_interactions.csv").open(
                "a",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=self._community_interactions_csv_fields,
                )
                writer.writerow(
                    {
                        field: row.get(field, "")
                        for field in self._community_interactions_csv_fields
                    }
                )
            with (self.run_dir / "traces" / "community_exposure_trace.jsonl").open(
                "a",
                encoding="utf-8",
            ) as f:
                f.write(
                    json.dumps(
                        {
                            "artifact": "community_exposure_trace",
                            **{
                                field: row.get(field, "")
                                for field in self._community_interactions_csv_fields
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
            self._community_provenance_ids.add(provenance_id)
        return True

    def _init_csv(self, path: Path, fieldnames: list[str], *, append_existing: bool = False) -> None:
        if append_existing and path.exists() and path.stat().st_size > 0:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                existing_header = next(csv.reader(f), [])
            if existing_header != fieldnames:
                raise RuntimeError(
                    f"Cannot resume with incompatible CSV schema: {path.name}. "
                    "Start a new run or use the original code version for this run."
                )
            return
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

    @staticmethod
    def _read_csv_rows(path: Path) -> list[dict[str, str]]:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _compact_agent(agent: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "agent_id",
            "user_type",
            "age",
            "gender",
            "strategy",
            "news_depth",
            "can_post_community",
            "segment_key",
        ]
        return {key: agent.get(key) for key in keys}


def finalize_community_delivery_counts(run_dir: Path | str) -> dict[str, int]:
    """Derive Best delivery counts from append-only AM exposure relations."""
    root = Path(run_dir)
    best_path = root / "community_best_posts.csv"
    interaction_path = root / "community_interactions.csv"
    if not best_path.exists() or best_path.stat().st_size == 0:
        return {
            "best_posts": 0,
            "delivered": 0,
            "right_censored": 0,
            "no_eligible_recipient": 0,
        }

    with best_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        best_rows = list(reader)
    if not best_rows:
        return {
            "best_posts": 0,
            "delivered": 0,
            "right_censored": 0,
            "no_eligible_recipient": 0,
        }
    required = {
        "date",
        "turn",
        "post_id",
        "scheduled_delivery_count",
        "actual_delivery_count",
        "delivery_status",
    }
    missing = required - set(fieldnames)
    if missing:
        raise RuntimeError(
            f"community_best_posts.csv lacks delivery fields: {sorted(missing)}"
        )

    interaction_rows: list[dict[str, str]] = []
    if interaction_path.exists() and interaction_path.stat().st_size > 0:
        with interaction_path.open("r", encoding="utf-8-sig", newline="") as f:
            interaction_rows = list(csv.DictReader(f))
    actual_by_best: Counter[tuple[str, str, str]] = Counter()
    for row in interaction_rows:
        if (
            str(row.get("delivery_status") or "") == "delivered_am"
            and str(row.get("is_best") or "").strip().lower() == "true"
        ):
            actual_by_best[
                (
                    str(row.get("source_date") or ""),
                    str(row.get("source_turn") or ""),
                    str(row.get("post_id") or ""),
                )
            ] += 1

    last_source_date = max(str(row.get("date") or "") for row in best_rows)
    delivered = 0
    right_censored = 0
    no_eligible_recipient = 0
    for row in best_rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("turn") or ""),
            str(row.get("post_id") or ""),
        )
        scheduled = int(row.get("scheduled_delivery_count") or 0)
        actual = int(actual_by_best.get(key, 0))
        row["actual_delivery_count"] = str(actual)
        if scheduled == 0 and actual == 0:
            row["delivery_status"] = "no_eligible_recipient"
            no_eligible_recipient += 1
        elif scheduled > 0 and actual == scheduled:
            row["delivery_status"] = "delivered_am"
            delivered += 1
        elif actual == 0 and str(row.get("date") or "") == last_source_date:
            row["delivery_status"] = "right_censored"
            right_censored += 1
        elif actual == 0:
            row["delivery_status"] = "missing_delivery"
        else:
            row["delivery_status"] = "partially_delivered"

    temporary = best_path.with_name(f".{best_path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(best_rows)
    temporary.replace(best_path)
    return {
        "best_posts": len(best_rows),
        "delivered": delivered,
        "right_censored": right_censored,
        "no_eligible_recipient": no_eligible_recipient,
    }


def _subturn_from_turn(turn: int) -> str:
    if turn <= 0:
        return "full"
    return "am" if turn % 2 == 1 else "pm"


def _news_reference_ids(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            result.append(str(item["id"]))
        elif isinstance(item, str) and item.startswith("news_"):
            result.append(item)
    return result
