use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct CliContract {
    pub tool: &'static str,
    pub contract_version: u32,
    pub summary: &'static str,
    pub global_flags: Vec<FlagContract>,
    pub scope: Option<ScopeContract>,
    pub commands: Vec<CommandContract>,
    pub error_codes: Vec<&'static str>,
}

#[derive(Debug, Serialize)]
pub struct CommandContract {
    pub name: &'static str,
    pub summary: &'static str,
    pub daemon_consumed: bool,
    pub positionals: Vec<PositionalContract>,
    pub flags: Vec<FlagContract>,
    pub json_output_keys: Vec<&'static str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub hard_dependencies: Vec<&'static str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub optional_dependencies: Vec<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub multimodal: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub degradation: Option<DegradationContract>,
}

impl CommandContract {
    pub fn new(name: &'static str, summary: &'static str) -> Self {
        assert!(!name.is_empty(), "command contract name must not be empty");
        assert!(
            !summary.is_empty(),
            "command contract summary must not be empty"
        );
        Self {
            name,
            summary,
            daemon_consumed: false,
            positionals: Vec::new(),
            flags: Vec::new(),
            json_output_keys: Vec::new(),
            hard_dependencies: Vec::new(),
            optional_dependencies: Vec::new(),
            multimodal: None,
            degradation: None,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct DegradationContract {
    pub output_shape: &'static str,
    pub metadata_keys: Vec<&'static str>,
}

#[derive(Debug, Serialize)]
pub struct FlagContract {
    pub name: &'static str,
    pub takes_value: bool,
    pub value_name: Option<&'static str>,
    pub allowed_values: Vec<&'static str>,
    pub required: bool,
    pub repeatable: bool,
}

#[derive(Debug, Serialize)]
pub struct PositionalContract {
    pub name: &'static str,
    pub required: bool,
    pub repeatable: bool,
}

#[derive(Debug, Serialize)]
pub struct ScopeContract {
    pub flags: Vec<FlagContract>,
    pub default: &'static str,
    pub identity_keys: Vec<&'static str>,
}

impl FlagContract {
    pub fn switch(name: &'static str) -> Self {
        Self {
            name,
            takes_value: false,
            value_name: None,
            allowed_values: Vec::new(),
            required: false,
            repeatable: false,
        }
    }

    pub fn value(name: &'static str, value_name: &'static str) -> Self {
        Self {
            name,
            takes_value: true,
            value_name: Some(value_name),
            allowed_values: Vec::new(),
            required: false,
            repeatable: false,
        }
    }

    pub fn repeatable_value(name: &'static str, value_name: &'static str) -> Self {
        Self {
            repeatable: true,
            ..Self::value(name, value_name)
        }
    }

    pub fn required(mut self) -> Self {
        self.required = true;
        self
    }

    pub fn allowed(mut self, values: Vec<&'static str>) -> Self {
        self.allowed_values = values;
        self
    }
}

impl PositionalContract {
    pub fn required(name: &'static str) -> Self {
        Self {
            name,
            required: true,
            repeatable: false,
        }
    }

    pub fn repeatable(name: &'static str) -> Self {
        Self {
            name,
            required: true,
            repeatable: true,
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    use super::*;

    #[test]
    fn command_contract_serializes_builder_shape_and_skips_empty_optional_fields() {
        let mut contract = CommandContract::new("index", "Index source files");
        contract.daemon_consumed = true;
        contract.positionals = vec![PositionalContract::repeatable("path")];
        contract.flags = vec![
            FlagContract::repeatable_value("glob", "GLOB")
                .required()
                .allowed(vec!["*.rs", "*.md"]),
            FlagContract::switch("dry-run"),
        ];
        contract.json_output_keys = vec!["indexed_files", "skipped_files"];

        let serialized = serde_json::to_string(&contract).expect("serialize command contract");

        assert_eq!(
            serialized,
            r#"{"name":"index","summary":"Index source files","daemon_consumed":true,"positionals":[{"name":"path","required":true,"repeatable":true}],"flags":[{"name":"glob","takes_value":true,"value_name":"GLOB","allowed_values":["*.rs","*.md"],"required":true,"repeatable":true},{"name":"dry-run","takes_value":false,"value_name":null,"allowed_values":[],"required":false,"repeatable":false}],"json_output_keys":["indexed_files","skipped_files"]}"#
        );

        let round_trip: Value =
            serde_json::from_str(&serialized).expect("golden command contract is valid JSON");
        assert!(round_trip.get("hard_dependencies").is_none());
        assert!(round_trip.get("optional_dependencies").is_none());
        assert!(round_trip.get("multimodal").is_none());
        assert!(round_trip.get("degradation").is_none());
        assert_eq!(round_trip["flags"][0]["value_name"].as_str(), Some("GLOB"));
        assert_eq!(round_trip["flags"][0]["required"].as_bool(), Some(true));
        assert_eq!(round_trip["flags"][0]["repeatable"].as_bool(), Some(true));
    }
}
