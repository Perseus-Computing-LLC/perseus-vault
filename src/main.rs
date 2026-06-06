mod db;
mod mcp;
mod tools;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "engram")]
#[command(
    about = "Persistent memory engine for Perseus — MCP JSON-RPC stdio server",
    version = "0.1.0"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,

    /// SQLite database path (used when no subcommand given)
    #[arg(long, default_value_t = default_db_path())]
    db: String,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the MCP JSON-RPC stdio server
    Serve {
        /// SQLite database path
        #[arg(long, default_value_t = default_db_path())]
        db: String,

        /// MCP mode (for compatibility — always on)
        #[arg(long, default_value_t = false)]
        mcp: bool,
    },
}

fn default_db_path() -> String {
    std::env::var("ENGRAM_DB_PATH").unwrap_or_else(|_| "engram.db".to_string())
}

fn main() {
    let cli = Cli::parse();

    // Determine db path based on subcommand or top-level flag
    let db_path = match &cli.command {
        Some(Commands::Serve { db, .. }) => db.clone(),
        None => {
            eprintln!("Usage: engram serve [--db PATH] [--mcp]");
            eprintln!("       The serve command starts the MCP JSON-RPC stdio server.");
            eprintln!("       Set ENGRAM_DB_PATH env var to override default database path.");
            std::process::exit(0);
        }
    };

    let database = match db::Database::open(&db_path) {
        Ok(db) => db,
        Err(e) => {
            eprintln!("engram-rs: failed to open database at {}: {}", db_path, e);
            std::process::exit(1);
        }
    };

    mcp::run_server(database);
}
