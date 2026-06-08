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

#[derive(Debug, Default, Serialize)]
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
