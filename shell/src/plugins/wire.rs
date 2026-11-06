// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The plugin host socket's frames, as `pylib/anki/remote.py` writes them:
//! `u32 length | u8 kind | u32 id | payload`, big endian, where the length
//! counts the kind, id and payload. A reply carries its request's id; id 0
//! is a request that wants no reply.

use std::io;
use std::io::Read;
use std::io::Write;

/// From the host: `u32 service | u32 method | protobuf input`.
pub const CALL: u8 = 1;
/// From the host: pylib's `db_command` JSON.
pub const DB: u8 = 2;
/// From the host: a JSON request to the shell (`{"op": ...}`).
pub const HOST: u8 = 3;
/// To the host: a JSON event (`{"event": ...}`).
pub const EVENT: u8 = 4;
pub const OK: u8 = 5;
/// `BackendError` bytes for CALL and DB, else UTF-8 text.
pub const ERR: u8 = 6;

/// Frames above this are refused rather than allocated.
const MAX_FRAME: usize = 1 << 30;

pub struct Frame {
    pub kind: u8,
    pub id: u32,
    pub payload: Vec<u8>,
}

pub fn write_frame(out: &mut impl Write, kind: u8, id: u32, payload: &[u8]) -> io::Result<()> {
    let len = u32::try_from(payload.len() + 5)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "frame too large"))?;
    let mut buf = Vec::with_capacity(payload.len() + 9);
    buf.extend_from_slice(&len.to_be_bytes());
    buf.push(kind);
    buf.extend_from_slice(&id.to_be_bytes());
    buf.extend_from_slice(payload);
    out.write_all(&buf)
}

/// The next frame, or None at a clean end of the stream.
pub fn read_frame(input: &mut impl Read) -> io::Result<Option<Frame>> {
    let mut len = [0u8; 4];
    match input.read_exact(&mut len) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(err) => return Err(err),
    }
    let len = u32::from_be_bytes(len) as usize;
    if !(5..=MAX_FRAME).contains(&len) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("bad frame length {len}"),
        ));
    }
    let mut body = vec![0u8; len];
    input.read_exact(&mut body)?;
    let id = u32::from_be_bytes(body[1..5].try_into().unwrap());
    Ok(Some(Frame {
        kind: body[0],
        id,
        payload: body.split_off(5),
    }))
}

/// A CALL payload's (service, method, input).
pub fn split_call(payload: &[u8]) -> Option<(u32, u32, &[u8])> {
    if payload.len() < 8 {
        return None;
    }
    let service = u32::from_be_bytes(payload[0..4].try_into().unwrap());
    let method = u32::from_be_bytes(payload[4..8].try_into().unwrap());
    Some((service, method, &payload[8..]))
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn frames_round_trip_and_end_cleanly() {
        let mut buf = vec![];
        write_frame(&mut buf, CALL, 7, b"\0\0\0\x01\0\0\0\x02input").unwrap();
        write_frame(&mut buf, OK, 0, b"").unwrap();
        let mut input = buf.as_slice();
        let frame = read_frame(&mut input).unwrap().unwrap();
        assert_eq!((frame.kind, frame.id), (CALL, 7));
        assert_eq!(
            split_call(&frame.payload),
            Some((1, 2, b"input".as_slice()))
        );
        let frame = read_frame(&mut input).unwrap().unwrap();
        assert_eq!((frame.kind, frame.id, frame.payload.len()), (OK, 0, 0));
        assert!(read_frame(&mut input).unwrap().is_none());
    }

    #[test]
    fn truncated_and_bogus_frames_are_errors() {
        let mut buf = vec![];
        write_frame(&mut buf, HOST, 1, b"{}").unwrap();
        assert!(read_frame(&mut &buf[..buf.len() - 1]).is_err());
        assert!(read_frame(&mut [0u8, 0, 0, 2, 1, 0].as_slice()).is_err());
        assert!(read_frame(&mut [0xffu8, 0xff, 0xff, 0xff].as_slice()).is_err());
        assert_eq!(split_call(b"short"), None);
    }
}
