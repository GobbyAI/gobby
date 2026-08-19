//! Tree-sitter heritage queries. Empty string means the language emits no edges.

pub(super) const HERITAGE_PYTHON: &str = r#"
(class_definition
  name: (identifier) @source
  superclasses: (argument_list [
    (identifier) @target
    (attribute) @target
  ])) @inherits
"#;

pub(super) const HERITAGE_JAVASCRIPT: &str = r#"
(class_declaration
  name: (identifier) @source
  (class_heritage [
    (identifier) @target
    (member_expression) @target
  ])) @inherits
"#;

pub(super) const HERITAGE_TYPESCRIPT: &str = r#"
(class_declaration
  name: (type_identifier) @source
  (class_heritage
    (extends_clause
      value: [
        (identifier) @target
        (type_identifier) @target
        (member_expression) @target
      ]))) @extends
(class_declaration
  name: (type_identifier) @source
  (class_heritage
    (implements_clause [
      (type_identifier) @target
      (generic_type) @target
      (nested_type_identifier) @target
    ]))) @implements
(interface_declaration
  name: (type_identifier) @source
  (extends_type_clause
    type: [
      (type_identifier) @target
      (generic_type) @target
      (nested_type_identifier) @target
    ])) @extends
"#;

pub(super) const HERITAGE_GO: &str = r#"
(type_spec
  name: (type_identifier) @source
  type: (struct_type
    (field_declaration_list
      (field_declaration
        !name
        type: [
          (type_identifier) @target
          (pointer_type (type_identifier) @target)
          (qualified_type) @target
        ])))) @extends
(type_spec
  name: (type_identifier) @source
  type: (interface_type
    (type_elem [
      (type_identifier) @target
      (qualified_type) @target
      (generic_type) @target
    ]))) @extends
"#;

pub(super) const HERITAGE_RUST: &str = r#"
(impl_item
  trait: [
    (type_identifier) @target
    (generic_type) @target
    (scoped_type_identifier) @target
  ]
  type: [
    (type_identifier) @source
    (generic_type) @source
    (scoped_type_identifier) @source
  ]) @implements
(trait_item
  name: (type_identifier) @source
  bounds: (trait_bounds [
    (type_identifier) @target
    (generic_type) @target
    (scoped_type_identifier) @target
  ])) @extends
"#;

pub(super) const HERITAGE_JAVA: &str = r#"
(class_declaration
  name: (identifier) @source
  superclass: (superclass [
    (type_identifier) @target
    (generic_type) @target
  ])) @extends
(class_declaration
  name: (identifier) @source
  interfaces: (super_interfaces
    (type_list [
      (type_identifier) @target
      (generic_type) @target
    ]))) @implements
(interface_declaration
  name: (identifier) @source
  (extends_interfaces
    (type_list [
      (type_identifier) @target
      (generic_type) @target
    ]))) @extends
"#;

pub(super) const HERITAGE_PHP: &str = r#"
(class_declaration
  name: (name) @source
  (base_clause [
    (name) @target
    (qualified_name) @target
  ])) @extends
(class_declaration
  name: (name) @source
  (class_interface_clause [
    (name) @target
    (qualified_name) @target
  ])) @implements
(interface_declaration
  name: (name) @source
  (base_clause [
    (name) @target
    (qualified_name) @target
  ])) @extends
"#;

pub(super) const HERITAGE_DART: &str = r#"
(class_declaration
  name: (identifier) @source
  superclass: (superclass
    type: (type (type_identifier) @target))) @extends
(class_declaration
  name: (identifier) @source
  interfaces: (interfaces
    (type (type_identifier) @target))) @implements
(class_declaration
  name: (identifier) @source
  superclass: (superclass
    (mixins
      (type (type_identifier) @target)))) @implements
"#;

pub(super) const HERITAGE_CSHARP: &str = r#"
(class_declaration
  name: (identifier) @source
  (base_list [
    (identifier) @target
    (generic_name) @target
    (qualified_name) @target
  ])) @class_base
(interface_declaration
  name: (identifier) @source
  (base_list [
    (identifier) @target
    (generic_name) @target
    (qualified_name) @target
  ])) @extends
(struct_declaration
  name: (identifier) @source
  (base_list [
    (identifier) @target
    (generic_name) @target
    (qualified_name) @target
  ])) @implements
"#;

pub(super) const HERITAGE_OBJC: &str = r#"
(class_interface
  (identifier) @source
  superclass: (identifier) @target) @extends
(class_interface
  (identifier) @source
  (protocol_reference_list (identifier) @target)) @implements
(protocol_declaration
  (identifier) @source
  (protocol_reference_list (identifier) @target)) @extends
"#;

pub(super) const HERITAGE_CPP: &str = r#"
(class_specifier
  name: (type_identifier) @source
  (base_class_clause [
    (type_identifier) @target
    (qualified_identifier) @target
    (template_type) @target
  ])) @extends
"#;

pub(super) const HERITAGE_RUBY: &str = r#"
(class
  name: (constant) @source
  superclass: (superclass [
    (constant) @target
    (scope_resolution) @target
  ])) @inherits
(class
  name: (constant) @source
  body: (body_statement
    (call
      method: (identifier) @mixin
      (#any-of? @mixin "include" "extend" "prepend")
      arguments: (argument_list [
        (constant) @target
        (scope_resolution) @target
      ])))) @implements
"#;

pub(super) const HERITAGE_KOTLIN: &str = r#"
(class_declaration
  name: (identifier) @source
  (delegation_specifiers
    (delegation_specifier [
      (constructor_invocation (user_type) @target)
      (user_type) @target
    ]))) @colon_base
"#;

pub(super) const HERITAGE_SCALA: &str = r#"
(class_definition
  name: [(identifier) (operator_identifier)] @source
  extend: (extends_clause
    type: [
      (identifier) @target
      (type_identifier) @target
      (stable_type_identifier) @target
      (generic_type) @target
    ])) @extends
(trait_definition
  name: [(identifier) (operator_identifier)] @source
  extend: (extends_clause
    type: [
      (identifier) @target
      (type_identifier) @target
      (stable_type_identifier) @target
      (generic_type) @target
    ])) @extends
"#;

pub(super) const HERITAGE_SWIFT: &str = r#"
(class_declaration
  declaration_kind: "class"
  name: (type_identifier) @source
  (inheritance_specifier
    inherits_from: (user_type (type_identifier) @target))) @colon_base
(class_declaration
  declaration_kind: "struct"
  name: (type_identifier) @source
  (inheritance_specifier
    inherits_from: (user_type (type_identifier) @target))) @implements
(protocol_declaration
  name: (type_identifier) @source
  (inheritance_specifier
    inherits_from: (user_type (type_identifier) @target))) @extends
"#;
