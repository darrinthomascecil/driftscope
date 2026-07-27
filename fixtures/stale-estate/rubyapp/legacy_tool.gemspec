Gem::Specification.new do |spec|
  spec.name          = "legacy-tool"
  spec.version       = "0.4.1"
  spec.summary       = "Fixture gemspec with stale declared dependencies."
  spec.required_ruby_version = ">= 2.7.0"

  spec.add_dependency "activesupport", "~> 6.1"
  spec.add_runtime_dependency "thor", "1.2.1"
  spec.add_development_dependency "rspec", "~> 3.11"
end
