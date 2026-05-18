# CoreInfraBuilder

The `CoreInfraBuilder` is a builder that is focused on infrastructure management. The main purpose of this builder is to provide a simple way to manage infrastructure resources of Exordos Core. The builder inherits from `UniversalBuilder` so you can override any method of the builder to provide custom logic. The builder extends the interface of the `UniversalBuilder` with some specific methods.

## create_infra

Create a list of infrastructure objects. The method returns a list of infrastructure objects that are required for the instance. For example, nodes, sets, configs, etc.
You don't need to implement the method `create_instance_derivatives` as `CoreInfraBuilder` implements it and uses the `create_infra` method to create derivatives.

## actualize_infra

Actualize the infrastructure objects. The method is called when the instance is outdated. For example, the instance `Config` has derivative `Render`. Single `Config` may have multiple `Render` derivatives. If any of the derivatives is outdated, this method is called to reactualize this infrastructure.

This method replaces the `actualize_outdated_instance_derivatives` method of the `UniversalBuilder`.
