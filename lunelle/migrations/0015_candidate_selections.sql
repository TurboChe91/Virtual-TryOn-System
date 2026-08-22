-- One operator-selected winner per candidate lineage. Selection is deliberately
-- separate from review_state: "best so far" is an editing decision, while human
-- approval is the publish/export gate.

CREATE TABLE candidate_selections (
    root_task_id       TEXT PRIMARY KEY REFERENCES tasks(task_id),
    selected_task_id   TEXT NOT NULL REFERENCES tasks(task_id),
    selected_by        TEXT NOT NULL,
    note               TEXT NOT NULL DEFAULT '',
    selected_at        TEXT NOT NULL
);

CREATE INDEX idx_candidate_selections_task
    ON candidate_selections(selected_task_id);
