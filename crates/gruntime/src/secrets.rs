//! Python-compatible daemon secret envelope and credential sealing.

use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, ErrorKind, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, URL_SAFE};
use fernet::Fernet;
use gobby_core::postgres_pool::Pool;
use openssl::md::Md;
use openssl::pkey::Id;
use openssl::pkey_ctx::PkeyCtx;
use openssl::symm::{Cipher, decrypt_aead, encrypt_aead};
use tokio::sync::OnceCell;
use uuid::Uuid;

const KEY_MATERIAL_ID: &str = "default";
const KEK_FILENAME: &str = ".secret_kek";
const PASSPHRASE_ENV: &str = "GOBBY_SECRET_KEK_PASSPHRASE";
const POSTURE_KEY_FILE: &str = "key_file";
const POSTURE_SCRYPT: &str = "scrypt_passphrase";
const SCRYPT_N: u64 = 1 << 14;
const SCRYPT_R: u64 = 8;
const SCRYPT_P: u64 = 1;
const SEAL_INFO: &[u8] = b"gobby.secret-store.seal.v1";
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;
const MIN_TOKEN_LEN: usize = 28;

#[derive(Debug, thiserror::Error)]
pub enum SecretError {
    #[error("secret key unavailable")]
    Unavailable,
    #[error("invalid sealed credential")]
    InvalidToken,
    #[error("stored secret cannot be decrypted")]
    Undecryptable,
    #[error("secret store database access failed")]
    Database(#[source] Box<dyn std::error::Error + Send + Sync>),
}

fn unavailable<E>(_: E) -> SecretError {
    SecretError::Unavailable
}

fn database(error: impl std::error::Error + Send + Sync + 'static) -> SecretError {
    SecretError::Database(Box::new(error))
}

pub struct SecretStore {
    pool: Arc<Pool>,
    home: PathBuf,
    kek_passphrase: Option<String>,
    envelope: OnceCell<Envelope>,
}

struct Envelope {
    cipher: Fernet,
    seal_key: [u8; 32],
}

impl Envelope {
    fn new(dek: &str) -> Result<Self, SecretError> {
        Ok(Self {
            cipher: Fernet::new(dek).ok_or(SecretError::Unavailable)?,
            seal_key: seal_key(dek)?,
        })
    }
}

struct KeyMaterial {
    wrapped_dek: String,
    posture: String,
    salt: Option<String>,
    n: Option<i32>,
    r: Option<i32>,
    p: Option<i32>,
}

impl SecretStore {
    pub fn new(pool: Arc<Pool>, home: PathBuf) -> Self {
        Self {
            pool,
            home,
            kek_passphrase: None,
            envelope: OnceCell::new(),
        }
    }

    pub fn with_kek_passphrase(mut self, passphrase: impl Into<String>) -> Self {
        self.kek_passphrase = Some(passphrase.into());
        self
    }

    pub async fn ensure_ready(&self) -> Result<(), SecretError> {
        self.envelope().await.map(|_| ())
    }

    pub async fn secret(&self, name: &str) -> Result<Option<String>, SecretError> {
        let Some(encrypted) = self.encrypted_value(&name.trim().to_lowercase()).await? else {
            return Ok(None);
        };
        let envelope = self.envelope().await?;
        let value = envelope
            .cipher
            .decrypt(&encrypted)
            .map_err(|_| SecretError::Undecryptable)?;
        String::from_utf8(value)
            .map(Some)
            .map_err(|_| SecretError::Undecryptable)
    }

    pub async fn seal(&self, plaintext: &[u8], aad: &[u8]) -> Result<String, SecretError> {
        let key = self.envelope().await?.seal_key;
        let plaintext = plaintext.to_vec();
        let aad = aad.to_vec();
        tokio::task::spawn_blocking(move || seal_with_key(&key, &plaintext, &aad))
            .await
            .map_err(unavailable)?
    }

    pub async fn open_sealed(&self, token: &str, aad: &[u8]) -> Result<Vec<u8>, SecretError> {
        let key = self.envelope().await?.seal_key;
        let token = token.to_owned();
        let aad = aad.to_vec();
        tokio::task::spawn_blocking(move || open_with_key(&key, &token, &aad))
            .await
            .map_err(unavailable)?
    }

    async fn envelope(&self) -> Result<&Envelope, SecretError> {
        self.envelope.get_or_try_init(|| self.load_envelope()).await
    }

    async fn load_envelope(&self) -> Result<Envelope, SecretError> {
        let dek = match self.key_material().await? {
            Some(material) => self.unwrap_dek(&material).await?,
            None => {
                // Python's guard: a missing envelope beside encrypted secrets is
                // corruption, and a fresh DEK would strand those secrets.
                if self.secrets_exist().await? {
                    return Err(SecretError::Unavailable);
                }
                self.initialize_dek().await?
            }
        };
        Envelope::new(&dek)
    }

    async fn encrypted_value(&self, name: &str) -> Result<Option<String>, SecretError> {
        let client = self.pool.get().await.map_err(database)?;
        let row = client
            .query_opt(
                "SELECT encrypted_value FROM secrets \
                 WHERE name = $1 AND project_id = '00000000-0000-0000-0000-000000000002'::uuid",
                &[&name],
            )
            .await
            .map_err(database)?;
        row.map(|row| row.try_get(0).map_err(database)).transpose()
    }

    async fn secrets_exist(&self) -> Result<bool, SecretError> {
        let client = self.pool.get().await.map_err(database)?;
        let row = client
            .query_opt("SELECT 1 FROM secrets LIMIT 1", &[])
            .await
            .map_err(database)?;
        Ok(row.is_some())
    }

    async fn key_material(&self) -> Result<Option<KeyMaterial>, SecretError> {
        let client = self.pool.get().await.map_err(database)?;
        let row = client
            .query_opt(
                "SELECT wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p \
                 FROM secret_key_material WHERE id = $1",
                &[&KEY_MATERIAL_ID],
            )
            .await
            .map_err(database)?;
        row.map(|row| -> Result<KeyMaterial, SecretError> {
            Ok(KeyMaterial {
                wrapped_dek: row.try_get(0).map_err(database)?,
                posture: row.try_get(1).map_err(database)?,
                salt: row.try_get(2).map_err(database)?,
                n: row.try_get(3).map_err(database)?,
                r: row.try_get(4).map_err(database)?,
                p: row.try_get(5).map_err(database)?,
            })
        })
        .transpose()
    }

    async fn initialize_dek(&self) -> Result<String, SecretError> {
        let kek = self.kek_file_or_create().await?;
        let dek = Fernet::generate_key();
        let wrapped = Fernet::new(&kek)
            .ok_or(SecretError::Unavailable)?
            .encrypt(dek.as_bytes());
        let client = self.pool.get().await.map_err(database)?;
        let inserted = client
            .query_opt(
                "INSERT INTO secret_key_material (id, wrapped_dek, kek_posture) \
                 VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING RETURNING id",
                &[&KEY_MATERIAL_ID, &wrapped, &POSTURE_KEY_FILE],
            )
            .await
            .map_err(database)?;
        drop(client);
        if inserted.is_some() {
            return Ok(dek);
        }
        // Lost the insert race: the winner's row is authoritative.
        let winner = self.key_material().await?.ok_or(SecretError::Unavailable)?;
        self.unwrap_dek(&winner).await
    }

    async fn unwrap_dek(&self, material: &KeyMaterial) -> Result<String, SecretError> {
        let kek = match material.posture.as_str() {
            POSTURE_KEY_FILE => self.existing_kek().await?,
            POSTURE_SCRYPT => self.scrypt_kek(material).await?,
            _ => return Err(SecretError::Unavailable),
        };
        let fernet = Fernet::new(&kek).ok_or(SecretError::Unavailable)?;
        let dek = fernet
            .decrypt(&material.wrapped_dek)
            .map_err(|_| SecretError::Unavailable)?;
        String::from_utf8(dek).map_err(unavailable)
    }

    async fn existing_kek(&self) -> Result<String, SecretError> {
        let home = self.home.clone();
        tokio::task::spawn_blocking(move || read_kek_file(&home.join(KEK_FILENAME)))
            .await
            .map_err(unavailable)??
            .ok_or(SecretError::Unavailable)
    }

    async fn kek_file_or_create(&self) -> Result<String, SecretError> {
        let home = self.home.clone();
        tokio::task::spawn_blocking(move || kek_file_or_create(&home))
            .await
            .map_err(unavailable)?
    }

    async fn scrypt_kek(&self, material: &KeyMaterial) -> Result<String, SecretError> {
        let passphrase = self
            .kek_passphrase
            .clone()
            .or_else(|| env::var(PASSPHRASE_ENV).ok())
            .filter(|passphrase| !passphrase.is_empty())
            .ok_or(SecretError::Unavailable)?;
        let salt = URL_SAFE
            .decode(material.salt.as_deref().ok_or(SecretError::Unavailable)?)
            .map_err(unavailable)?;
        let n = kdf_param(material.n, SCRYPT_N)?;
        let r = kdf_param(material.r, SCRYPT_R)?;
        let p = kdf_param(material.p, SCRYPT_P)?;
        tokio::task::spawn_blocking(move || derive_scrypt_kek(&passphrase, &salt, n, r, p))
            .await
            .map_err(unavailable)?
    }
}

fn kdf_param(value: Option<i32>, default: u64) -> Result<u64, SecretError> {
    match value {
        None => Ok(default),
        Some(value) => u64::try_from(value).map_err(unavailable),
    }
}

fn derive_scrypt_kek(
    passphrase: &str,
    salt: &[u8],
    n: u64,
    r: u64,
    p: u64,
) -> Result<String, SecretError> {
    let maxmem = 128u64
        .checked_mul(r)
        .and_then(|v| v.checked_mul(n.checked_add(p)?.checked_add(2)?))
        .ok_or(SecretError::Unavailable)?;
    let mut key = [0u8; 32];
    openssl::pkcs5::scrypt(passphrase.as_bytes(), salt, n, r, p, maxmem, &mut key)
        .map_err(unavailable)?;
    Ok(URL_SAFE.encode(key))
}

fn seal_key(dek: &str) -> Result<[u8; 32], SecretError> {
    let ikm = URL_SAFE.decode(dek).map_err(unavailable)?;
    let mut context = PkeyCtx::new_id(Id::HKDF).map_err(unavailable)?;
    context.derive_init().map_err(unavailable)?;
    context.set_hkdf_md(Md::sha256()).map_err(unavailable)?;
    context.set_hkdf_key(&ikm).map_err(unavailable)?;
    context.add_hkdf_info(SEAL_INFO).map_err(unavailable)?;
    let mut key = [0u8; 32];
    context.derive(Some(&mut key)).map_err(unavailable)?;
    Ok(key)
}

fn seal_with_key(key: &[u8; 32], plaintext: &[u8], aad: &[u8]) -> Result<String, SecretError> {
    let mut nonce = [0u8; NONCE_LEN];
    openssl::rand::rand_bytes(&mut nonce).map_err(unavailable)?;
    let mut tag = [0u8; TAG_LEN];
    let ciphertext = encrypt_aead(
        Cipher::aes_256_gcm(),
        key,
        Some(&nonce),
        aad,
        plaintext,
        &mut tag,
    )
    .map_err(unavailable)?;
    let mut token = Vec::with_capacity(NONCE_LEN + ciphertext.len() + TAG_LEN);
    token.extend_from_slice(&nonce);
    token.extend_from_slice(&ciphertext);
    token.extend_from_slice(&tag);
    Ok(STANDARD.encode(token))
}

fn open_with_key(key: &[u8; 32], token: &str, aad: &[u8]) -> Result<Vec<u8>, SecretError> {
    let raw = STANDARD
        .decode(token)
        .map_err(|_| SecretError::InvalidToken)?;
    if raw.len() < MIN_TOKEN_LEN {
        return Err(SecretError::InvalidToken);
    }
    let (nonce, rest) = raw.split_at(NONCE_LEN);
    let (ciphertext, tag) = rest.split_at(rest.len() - TAG_LEN);
    decrypt_aead(
        Cipher::aes_256_gcm(),
        key,
        Some(nonce),
        aad,
        ciphertext,
        tag,
    )
    .map_err(|_| SecretError::InvalidToken)
}

/// Reads the key-file KEK, enforcing 0600 as Python's `_read_kek_file` does.
fn read_kek_file(path: &Path) -> Result<Option<String>, SecretError> {
    let raw = match fs::read(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(SecretError::Unavailable),
    };
    let key = std::str::from_utf8(raw.trim_ascii())
        .map_err(unavailable)?
        .to_owned();
    if Fernet::new(&key).is_none() {
        return Err(SecretError::Unavailable);
    }
    // Python only warns when the chmod fails; keep reading the same way.
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    Ok(Some(key))
}

fn kek_file_or_create(home: &Path) -> Result<String, SecretError> {
    fs::create_dir_all(home).map_err(unavailable)?;
    let path = home.join(KEK_FILENAME);
    if let Some(kek) = read_kek_file(&path)? {
        return Ok(kek);
    }
    let kek = Fernet::generate_key();
    if publish_private_file(&path, kek.as_bytes()).map_err(unavailable)? {
        return Ok(kek);
    }
    // A racing initializer won the link; adopt its file.
    read_kek_file(&path)?.ok_or(SecretError::Unavailable)
}

/// Publishes `data` at `path` with mode 0600 and never replaces an existing file.
/// Returns `false` when another publisher won the link.
pub fn publish_private_file(path: &Path, data: &[u8]) -> io::Result<bool> {
    let directory = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidInput, "path has no file name"))?;
    let temp = directory.join(format!(
        ".{}.{}.{}.tmp",
        name.to_string_lossy(),
        std::process::id(),
        Uuid::new_v4().simple()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    let outcome = write_and_link(&mut file, data, &temp, path);
    drop(file);
    let removed = fs::remove_file(&temp);
    let published = outcome?;
    match removed {
        Err(error) if error.kind() != ErrorKind::NotFound => return Err(error),
        _ => {}
    }
    if published {
        File::open(directory)?.sync_all()?;
    }
    Ok(published)
}

fn write_and_link(file: &mut File, data: &[u8], temp: &Path, path: &Path) -> io::Result<bool> {
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    file.write_all(data)?;
    file.sync_all()?;
    // A hard link is an atomic, no-clobber publication within one directory.
    match fs::hard_link(temp, path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == ErrorKind::AlreadyExists => Ok(false),
        Err(error) => Err(error),
    }
}
