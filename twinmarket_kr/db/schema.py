from __future__ import annotations


AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    source_user_id TEXT NOT NULL,
    user_type TEXT NOT NULL,
    gender TEXT NOT NULL,
    age INTEGER NOT NULL,
    age_group TEXT NOT NULL,
    location TEXT NOT NULL,
    bh_disposition_effect_category TEXT NOT NULL,
    bh_lottery_preference_category TEXT NOT NULL,
    bh_total_return_category TEXT NOT NULL,
    bh_underdiversification_category TEXT NOT NULL,
    strategy TEXT NOT NULL,
    trad_pro INTEGER NOT NULL DEFAULT 0,
    fol_ind TEXT NOT NULL,
    ini_cash INTEGER NOT NULL,
    news_depth INTEGER NOT NULL DEFAULT 1,
    segment_key TEXT NOT NULL,
    match_score INTEGER NOT NULL,
    persona_prompt TEXT NOT NULL,
    momentum_contrarian TEXT NOT NULL DEFAULT 'neutral'
        CHECK(momentum_contrarian IN ('momentum', 'contrarian', 'neutral')),
    can_post_community INTEGER NOT NULL DEFAULT 0
        CHECK(can_post_community IN (0, 1))
);
"""

SIM_DDLS = [
    """
    CREATE TABLE IF NOT EXISTS belief_history (
        belief_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        dim_1 TEXT,
        dim_2 TEXT,
        dim_3 TEXT,
        dim_4 TEXT,
        dim_5 TEXT,
        dim_6 TEXT,
        belief_summary TEXT NOT NULL,
        view_change TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_state (
        state_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        cash REAL NOT NULL,
        positions TEXT NOT NULL,
        total_value REAL NOT NULL,
        realized_pnl REAL NOT NULL,
        total_return_rate REAL NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_log (
        log_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        action TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        executed_price REAL,
        trade_value REAL,
        fee REAL NOT NULL,
        action_reason TEXT,
        risk_control TEXT,
        order_type TEXT,
        submitted_price REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        filled_quantity INTEGER NOT NULL DEFAULT 0,
        analysis_id TEXT REFERENCES simulation_analyses(analysis_id),
        decision_id TEXT REFERENCES simulation_decisions(decision_id),
        source_ltb_id TEXT REFERENCES simulation_ltb_states(ltb_id),
        source_stb_id TEXT REFERENCES simulation_stb_states(stb_id),
        fill_id TEXT REFERENCES simulation_fills(fill_id),
        post_fill_ltb_id TEXT REFERENCES simulation_ltb_states(ltb_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_stb_states (
        stb_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        dim_1 TEXT NOT NULL,
        dim_2 TEXT NOT NULL,
        dim_3 TEXT NOT NULL,
        dim_4 TEXT NOT NULL,
        dim_5 TEXT NOT NULL,
        dim_6 TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        dimension_evidence_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, turn)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_ltb_states (
        ltb_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn >= 0),
        visible_from_turn INTEGER NOT NULL CHECK(visible_from_turn = turn + 1),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('initial', 'am', 'pm')),
        parent_ltb_id TEXT REFERENCES simulation_ltb_states(ltb_id),
        source_stb_id TEXT REFERENCES simulation_stb_states(stb_id),
        source_decision_id TEXT REFERENCES simulation_decisions(decision_id),
        source_fill_id TEXT REFERENCES simulation_fills(fill_id),
        dim_1 TEXT NOT NULL,
        dim_2 TEXT NOT NULL,
        dim_3 TEXT NOT NULL,
        dim_4 TEXT NOT NULL,
        dim_5 TEXT NOT NULL,
        dim_6 TEXT NOT NULL,
        integration_evidence_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        belief_summary TEXT NOT NULL,
        view_change_json TEXT NOT NULL,
        human_log_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, turn),
        CHECK(
            (
                turn = 0
                AND subturn = 'initial'
                AND parent_ltb_id IS NULL
                AND source_stb_id IS NULL
                AND source_decision_id IS NULL
                AND source_fill_id IS NULL
            )
            OR
            (
                turn > 0
                AND subturn IN ('am', 'pm')
                AND parent_ltb_id IS NOT NULL
                AND source_stb_id IS NOT NULL
                AND source_decision_id IS NOT NULL
                AND source_fill_id IS NOT NULL
            )
        )
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_simulation_ltb_visible
    ON simulation_ltb_states(agent_id, visible_from_turn, turn);
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_analyses (
        analysis_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        source_ltb_id TEXT NOT NULL REFERENCES simulation_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES simulation_stb_states(stb_id),
        analysis_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, turn)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_decisions (
        decision_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        action TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
        requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
        source_ltb_id TEXT NOT NULL REFERENCES simulation_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES simulation_stb_states(stb_id),
        analysis_id TEXT NOT NULL UNIQUE
            REFERENCES simulation_analyses(analysis_id),
        decision_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, turn)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_fills (
        fill_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        stock_code TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
        requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
        filled_quantity INTEGER NOT NULL CHECK(filled_quantity = requested_quantity),
        executed_price REAL NOT NULL CHECK(executed_price > 0),
        fee REAL NOT NULL DEFAULT 0 CHECK(fee = 0),
        source_ltb_id TEXT NOT NULL REFERENCES simulation_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES simulation_stb_states(stb_id),
        decision_id TEXT NOT NULL REFERENCES simulation_decisions(decision_id),
        pre_portfolio_json TEXT NOT NULL,
        post_portfolio_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, turn),
        UNIQUE(decision_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_trade_outcomes (
        outcome_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL REFERENCES simulation_fills(fill_id),
        horizon TEXT NOT NULL CHECK(horizon IN ('next_turn', 'h1', 'h5')),
        due_event_id TEXT,
        available_from_event_id TEXT,
        observed_event_id TEXT,
        mark_price REAL CHECK(mark_price IS NULL OR mark_price > 0),
        status TEXT NOT NULL CHECK(status IN ('matured', 'right_censored')),
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(fill_id, horizon),
        CHECK(
            (
                status = 'matured'
                AND due_event_id IS NOT NULL
                AND available_from_event_id = due_event_id
                AND observed_event_id = due_event_id
                AND mark_price IS NOT NULL
            )
            OR
            (
                status = 'right_censored'
                AND due_event_id IS NULL
                AND available_from_event_id IS NULL
                AND observed_event_id IS NULL
                AND mark_price IS NULL
            )
        )
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_simulation_outcomes_available
    ON simulation_trade_outcomes(available_from_event_id, status);
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_outcome_consumptions (
        consumption_id TEXT PRIMARY KEY,
        outcome_id TEXT NOT NULL UNIQUE
            REFERENCES simulation_trade_outcomes(outcome_id),
        fill_id TEXT NOT NULL,
        horizon TEXT NOT NULL CHECK(horizon IN ('next_turn', 'h1', 'h5')),
        ltb_id TEXT NOT NULL REFERENCES simulation_ltb_states(ltb_id),
        consumed_at_event_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(fill_id, horizon),
        FOREIGN KEY(fill_id, horizon)
            REFERENCES simulation_trade_outcomes(fill_id, horizon)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_simulation_outcome_consumptions_ltb
    ON simulation_outcome_consumptions(ltb_id, consumed_at_event_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_system_messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        message_type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS StockData (
        date TEXT NOT NULL,
        stock_id TEXT NOT NULL,
        open_price REAL,
        high_price REAL,
        low_price REAL,
        close_price REAL NOT NULL,
        volume REAL,
        pct_chg REAL,
        volume_chg REAL,
        ma5 REAL,
        ma20 REAL,
        volatility_20d REAL,
        PRIMARY KEY (date, stock_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS TradingDetails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        stock_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        trading_direction TEXT NOT NULL,
        price REAL NOT NULL,
        volume INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_posts (
        post_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        anonymous_code TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        post_type TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        like_count INTEGER NOT NULL DEFAULT 0,
        unlike_count INTEGER NOT NULL DEFAULT 0,
        score INTEGER NOT NULL DEFAULT 0,
        is_best INTEGER NOT NULL DEFAULT 0,
        source_ltb_id TEXT REFERENCES simulation_ltb_states(ltb_id),
        source_fill_id TEXT REFERENCES simulation_fills(fill_id),
        source_decision_id TEXT REFERENCES simulation_decisions(decision_id)
    );
    """,
    # 한 agent는 한 PM에 최대 한 글만 쓴다. 테이블 정의에 UNIQUE를 넣으면
    # ``CREATE TABLE IF NOT EXISTS`` 때문에 신규 DB만 제약을 갖게 되므로,
    # 기존 DB에도 같은 제약이 적용되는 unique index로 강제한다.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_community_posts_agent_date
        ON community_posts (agent_id, date);
    """,
    """
    CREATE TABLE IF NOT EXISTS community_interactions (
        interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        post_id INTEGER NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        reaction TEXT NOT NULL,
        UNIQUE(agent_id, post_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        best_posts_seen TEXT,
        posts_read TEXT,
        candidate_posts_seen TEXT NOT NULL DEFAULT '[]',
        community_thinking TEXT,
        UNIQUE(agent_id, turn)
    );
    """,
]

def create_agents_table_sql() -> str:
    return AGENTS_DDL


def create_sim_tables_sql() -> list[str]:
    """Return the canonical schema for the integrated simulation runtime."""
    return SIM_DDLS
