use postgres::Client;

pub struct ProjectCleanup {
    database_url: String,
    project_id: String,
}

impl ProjectCleanup {
    pub fn new(database_url: &str, project_id: &str) -> Self {
        Self {
            database_url: database_url.to_string(),
            project_id: project_id.to_string(),
        }
    }
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        let database_url = self.database_url.clone();
        let project_id = self.project_id.clone();
        if std::panic::catch_unwind(move || {
			let mut conn = match gobby_core::postgres::connect_readwrite(&database_url) {
				Ok(conn) => conn,
				Err(err) => {
					eprintln!(
						"ProjectCleanup cleanup: connect_readwrite failed for project {project_id}: {err}",
					);
					return;
				}
			};

			if let Err(err) = cleanup_project(&mut conn, &project_id) {
				eprintln!(
					"ProjectCleanup cleanup: cleanup_project failed for project {project_id}: {err}",
				);
			}
		})
		.is_err()
		{
			eprintln!("ProjectCleanup cleanup panicked for project cleanup");
		}
    }
}

pub fn cleanup_project(conn: &mut Client, project_id: &str) -> Result<(), postgres::Error> {
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.commit()
}
