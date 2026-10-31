pub mod fake_hub;
// Other integration binaries include this shared module without host tests.
#[expect(dead_code, reason = "shared fixture is exercised by node_agents")]
pub mod fake_gterm;

mod node_home;
pub use node_home::{NODE_KEY, NodeHome};
