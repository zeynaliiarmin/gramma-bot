CREATE TABLE auto_replies (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	keywords TEXT NOT NULL, 
	reply TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	matches BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_auto_replies_account_id ON auto_replies (account_id);

CREATE TABLE automation_scenarios (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	match_mode VARCHAR(12) NOT NULL, 
	trigger_text TEXT NOT NULL, 
	ai_instruction TEXT NOT NULL, 
	fallback_reply TEXT NOT NULL, 
	use_ai BOOLEAN NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	priority INTEGER NOT NULL, 
	hits BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_automation_scenarios_account_id ON automation_scenarios (account_id);

CREATE TABLE automation_sessions (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	peer_id VARCHAR(128) NOT NULL, 
	scenario_id BIGINT NOT NULL, 
	step_order INTEGER NOT NULL, 
	collected TEXT NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_automation_sessions_scenario_id ON automation_sessions (scenario_id);

CREATE INDEX ix_automation_sessions_account_id ON automation_sessions (account_id);

CREATE TABLE chat_history (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	peer_id VARCHAR(128) NOT NULL, 
	peer_name VARCHAR(255) NOT NULL, 
	inbound_text TEXT NOT NULL, 
	reply_text TEXT NOT NULL, 
	reply_source VARCHAR(24) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_chat_history_owner_channel_created ON chat_history (account_id, channel, created_at);

CREATE INDEX ix_chat_history_account_id ON chat_history (account_id);

CREATE TABLE collab_requests (
	id BIGSERIAL NOT NULL, 
	owner_id BIGINT NOT NULL, 
	partner_id BIGINT NOT NULL, 
	source_account_id BIGINT NOT NULL, 
	target_account_id BIGINT NOT NULL, 
	post_id BIGINT, 
	message TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	responded_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_collab_requests_owner_id ON collab_requests (owner_id);

CREATE INDEX ix_collab_requests_status ON collab_requests (status);

CREATE INDEX ix_collab_requests_partner_id ON collab_requests (partner_id);

CREATE INDEX ix_collab_requests_target_account_id ON collab_requests (target_account_id);

CREATE INDEX ix_collab_requests_source_account_id ON collab_requests (source_account_id);

CREATE TABLE design_history (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT, 
	kind VARCHAR(24) NOT NULL, 
	prompt TEXT NOT NULL, 
	content TEXT NOT NULL, 
	engine VARCHAR(24) NOT NULL, 
	language VARCHAR(8) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_design_history_owner_created ON design_history (account_id, created_at);

CREATE INDEX ix_design_history_account_id ON design_history (account_id);

CREATE TABLE instagram_accounts (
	id BIGSERIAL NOT NULL, 
	owner_id BIGINT NOT NULL, 
	instagram_user_id VARCHAR(64), 
	username VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	profile_pic_url TEXT NOT NULL, 
	long_lived_token_enc TEXT, 
	token_expires_at TIMESTAMP WITH TIME ZONE, 
	key_version INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	last_sync_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_instagram_accounts_instagram_user_id ON instagram_accounts (instagram_user_id);

CREATE INDEX ix_instagram_accounts_owner_id ON instagram_accounts (owner_id);

CREATE TABLE post_reminders (
	id BIGSERIAL NOT NULL, 
	post_id BIGINT NOT NULL, 
	owner_id BIGINT NOT NULL, 
	remind_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	message TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_post_reminder_post UNIQUE (post_id)
);

CREATE INDEX ix_post_reminders_due ON post_reminders (remind_at, sent_at);

CREATE INDEX ix_post_reminders_post_id ON post_reminders (post_id);

CREATE INDEX ix_post_reminders_remind_at ON post_reminders (remind_at);

CREATE INDEX ix_post_reminders_owner_id ON post_reminders (owner_id);

CREATE TABLE post_templates (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	caption TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_post_templates_account_id ON post_templates (account_id);

CREATE TABLE users (
	id BIGINT NOT NULL, 
	telegram_username VARCHAR(64), 
	full_name VARCHAR(255) NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	locale VARCHAR(8) NOT NULL, 
	active_account_id BIGINT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE activity_logs (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT, 
	user_id BIGINT, 
	action VARCHAR(64) NOT NULL, 
	detail TEXT NOT NULL, 
	level VARCHAR(8) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_activity_logs_action ON activity_logs (action);

CREATE INDEX ix_activity_logs_user_id ON activity_logs (user_id);

CREATE INDEX ix_activity_logs_account_id ON activity_logs (account_id);

CREATE TABLE automation_steps (
	id BIGSERIAL NOT NULL, 
	scenario_id BIGINT NOT NULL, 
	order_index INTEGER NOT NULL, 
	action VARCHAR(12) NOT NULL, 
	text TEXT NOT NULL, 
	use_ai BOOLEAN NOT NULL, 
	next_step_order INTEGER, 
	variable VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_automation_steps_scenario_id ON automation_steps (scenario_id);

CREATE TABLE comments (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	ig_comment_id VARCHAR(64) NOT NULL, 
	ig_media_id VARCHAR(64) NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	text TEXT NOT NULL, 
	replied BOOLEAN NOT NULL, 
	hidden BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_comments_account_id ON comments (account_id);

CREATE INDEX ix_comments_ig_comment_id ON comments (ig_comment_id);

CREATE TABLE conversations (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	ig_thread_id VARCHAR(64) NOT NULL, 
	counterpart_name VARCHAR(255) NOT NULL, 
	last_message_preview TEXT NOT NULL, 
	category VARCHAR(24) NOT NULL, 
	unread_count INTEGER NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_conversations_account_id ON conversations (account_id);

CREATE INDEX ix_conversations_ig_thread_id ON conversations (ig_thread_id);

CREATE TABLE oauth_flows (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	code_enc TEXT NOT NULL, 
	code_verifier TEXT NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_oauth_flow_account UNIQUE (account_id)
);

CREATE INDEX ix_oauth_flows_account_id ON oauth_flows (account_id);

CREATE TABLE posts (
	id BIGSERIAL NOT NULL, 
	account_id BIGINT NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	caption TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	scheduled_at TIMESTAMP WITH TIME ZONE, 
	publish_started_at TIMESTAMP WITH TIME ZONE, 
	published_at TIMESTAMP WITH TIME ZONE, 
	ig_media_id VARCHAR(64), 
	ig_permalink TEXT NOT NULL, 
	error_message TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_posts_scheduled_at ON posts (scheduled_at);

CREATE INDEX ix_posts_account_id ON posts (account_id);

CREATE TABLE media (
	id BIGSERIAL NOT NULL, 
	post_id BIGINT, 
	url TEXT NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	order_index INTEGER NOT NULL, 
	container_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_media_post_id ON media (post_id);

ALTER TABLE instagram_accounts ADD CONSTRAINT instagram_accounts_owner_id_fkey FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE;

ALTER TABLE users ADD CONSTRAINT users_active_account_id_fkey FOREIGN KEY(active_account_id) REFERENCES instagram_accounts (id) ON DELETE SET NULL;

ALTER TABLE activity_logs ADD CONSTRAINT activity_logs_account_id_fkey FOREIGN KEY(account_id) REFERENCES instagram_accounts (id) ON DELETE CASCADE;

ALTER TABLE automation_steps ADD CONSTRAINT automation_steps_scenario_id_fkey FOREIGN KEY(scenario_id) REFERENCES automation_scenarios (id) ON DELETE CASCADE;

ALTER TABLE comments ADD CONSTRAINT comments_account_id_fkey FOREIGN KEY(account_id) REFERENCES instagram_accounts (id) ON DELETE CASCADE;

ALTER TABLE conversations ADD CONSTRAINT conversations_account_id_fkey FOREIGN KEY(account_id) REFERENCES instagram_accounts (id) ON DELETE CASCADE;

ALTER TABLE oauth_flows ADD CONSTRAINT oauth_flows_account_id_fkey FOREIGN KEY(account_id) REFERENCES instagram_accounts (id) ON DELETE CASCADE;

ALTER TABLE posts ADD CONSTRAINT posts_account_id_fkey FOREIGN KEY(account_id) REFERENCES instagram_accounts (id) ON DELETE CASCADE;

ALTER TABLE media ADD CONSTRAINT media_post_id_fkey FOREIGN KEY(post_id) REFERENCES posts (id) ON DELETE CASCADE;

CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_instagram_accounts_updated_at ON instagram_accounts;
CREATE TRIGGER trg_instagram_accounts_updated_at BEFORE UPDATE ON instagram_accounts FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

