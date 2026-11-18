-- How each brief was produced, and every tool call the research agent made.
ALTER TABLE briefs ADD COLUMN mode text NOT NULL DEFAULT 'template'
    CHECK (mode IN ('template', 'agent'));

CREATE TABLE agent_steps (
    brief_id bigint NOT NULL REFERENCES briefs(id),
    n        int NOT NULL,
    tool     text NOT NULL,
    input    text NOT NULL,
    result   text NOT NULL,
    ok       boolean NOT NULL,
    PRIMARY KEY (brief_id, n)
);
