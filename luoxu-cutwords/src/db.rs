use std::mem::swap;
use std::time;

use eyre::{eyre, Result};
use postgres::{Client, Statement};
use tracing::info;

struct Message {
    msgid: i64,
    text: String,
}

pub struct MessageIter {
    client: Client,
    statement: Statement,
    errored: bool,
    done: bool,

    rows: Vec<Message>,
    last_idx: usize,
    last_msgid: i64,

    group_id: i64,
    endtime: time::SystemTime,
    user_id: Option<i64>,
}

impl MessageIter {
    pub fn new(
        mut client: Client,
        group_id: i64,
        endtime: u64,
        user_id: Option<i64>,
    ) -> Result<Self> {
        let archive: String = client
            .query_opt(
                "SELECT replace(c.archive_id::text, '-', '') FROM tg_groups g \
       JOIN conversations c ON c.id=g.conversation_id WHERE g.group_id=$1",
                &[&group_id],
            )?
            .ok_or_else(|| eyre!("group not found: {}", group_id))?
            .get(0);
        let table = message_table(&archive)?;
        let statement = client.prepare(
            &match user_id {
                Some(_) => {
                    r"
          SELECT msgid, text FROM {messages}
          WHERE msgid < $1
            and group_id = $2
            and created_at > $3
            and from_user = $4
            and deleted_at IS NULL
          ORDER BY msgid DESC LIMIT 1000
        "
                }
                None => {
                    r"
          SELECT msgid, text FROM {messages}
          WHERE msgid < $1
            and group_id = $2
            and created_at > $3
            and deleted_at IS NULL
          ORDER BY msgid DESC LIMIT 1000
        "
                }
            }
            .replace("{messages}", &table),
        )?;
        Ok(MessageIter {
            client,
            statement,
            errored: false,
            done: false,

            rows: Vec::new(),
            last_idx: 0,
            last_msgid: i64::MAX,

            group_id,
            endtime: time::SystemTime::UNIX_EPOCH + time::Duration::from_secs(endtime),
            user_id,
        })
    }
}

fn message_table(key: &str) -> Result<String> {
    // Only a registry UUID's hex form can become an SQL identifier.
    if key.len() != 32 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(eyre!("invalid archive UUID"));
    }
    Ok(format!("\"messages_{}\"", key))
}

#[cfg(test)]
mod tests {
    use super::message_table;

    #[test]
    fn archive_identifiers_are_uuid_hex_only() {
        assert_eq!(
            message_table("0123456789abcdef0123456789abcdef").unwrap(),
            "\"messages_0123456789abcdef0123456789abcdef\""
        );
        for invalid in [
            "",
            "123",
            "not-a-uuid",
            "\"; DROP TABLE messages; --0000000",
        ] {
            assert!(message_table(invalid).is_err());
        }
    }
}

impl Iterator for MessageIter {
    type Item = Result<String>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.errored {
            return None;
        }

        if self.last_idx == self.rows.len() {
            if self.done {
                return None;
            }

            info!("query database for messages");
            let rows = match self.user_id {
                Some(uid) => self.client.query(
                    &self.statement,
                    &[&self.last_msgid, &self.group_id, &self.endtime, &uid],
                ),
                None => self.client.query(
                    &self.statement,
                    &[&self.last_msgid, &self.group_id, &self.endtime],
                ),
            };

            if let Err(e) = rows {
                self.errored = true;
                return Some(Err(e.into()));
            }
            let rows = rows.unwrap();

            let messages: Vec<Message> = rows
                .iter()
                .map(|row| Message {
                    msgid: row.get(0),
                    text: row.get(1),
                })
                .collect();
            if messages.len() < 1000 {
                self.done = true;
            }
            self.last_idx = 0;
            self.rows = messages;
            if self.rows.is_empty() {
                return None;
            } else {
                self.last_msgid = self.rows[self.rows.len() - 1].msgid;
            }
        }

        let mut ret = String::new();
        swap(&mut self.rows[self.last_idx].text, &mut ret);
        self.last_idx += 1;
        Some(Ok(ret))
    }
}
