# Universal Builder Quick start

This page provides a quick start guide for the Universal Builder. How to implement own builder based on the universal builder.

Most of the interface methods have default implementation so you can override only methods you need but for the examples we will override minimal set of methods.

Common plan to implement own builder based on the universal builder:

1. Create an instance model that inherits from `InstanceMixin` or `InstanceWithDerivativesMixin`.
2. Create a builder that inherits from `UniversalBuilder`.
3. Implement minimal set of methods.

## CoreInfraBuilder

For the example we will implement `CoreInfraBuilder` that will build infrastructure for the instance `PGInstance`. The instance `PGInstance` is a model that represents a cluster of PostgreSQL. For simplicity we will skip the part that configure the PostgreSQL cluster and focus on the infrastructure part.

### Instance

The first step is to create an instance model that inherits from `InstanceMixin` or `InstanceWithDerivativesMixin`.

```python
class PGInstance(models.PGInstance, ua_models.InstanceWithDerivativesMixin):

    __derivative_model_map__ = {
        "node": sdk_models.Node,
    }

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "pg_instance"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Plain names (``"name"``) or dot separated paths
        (``"setter.kind"``) that descend through nested dicts and lists.
        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "nodes_number",
                "project_id",
            )
        )
    
    def create_infra(
        self,
    ) -> tp.Collection[sdk_models.Node]:
        """Get the infrastructure for the instance."""
        infra = []
        
        for i in range(self.nodes_number):
            infra.append(
                sdk_models.Node(
                    uuid=str(uuid.uuid4()),
                    name=f"{self.name}-node-{i}",
                    cores=2,
                    memory=4096,
                )
            )
        
        return infra
```

The most interesting part in the snippet above:

- `models.PGInstance` is main user model that represents a cluster of PostgreSQL.
- `ua_models.InstanceWithDerivativesMixin` is a mixin that provides the necessary methods to work with derivatives. It means our instance has derivatives.
- `__derivative_model_map__` is a dictionary that maps derivative models to their kinds. In our simple case we have one derivative: `Node`.
- `get_resource_kind` returns the resource kind.
- `get_resource_target_fields` returns the collection of target fields.

### Builder

The second step is to create a builder that inherits from `UniversalBuilder`. We will override only minimal set of methods:

- `create_instance_derivatives` - create derivatives for the instance.
- `actualize_outdated_instance_derivatives` - actualize derivatives for the instance.

```python
class CoreInfraBuilder(builder.UniversalAgentBuilderService):
    def create_instance_derivatives(
        self, instance: PGInstance
    ) -> tp.Collection[sdk_models.Node]:
        return instance.create_infra()
    
    def actualize_outdated_instance_derivatives(
        self,
        instance: PGInstance,
        derivative_pairs: tp.Collection[
            tuple[
                sdk_models.Node,         # The target resource
                sdk_models.Node | None,  # The actual resource
            ]
        ],
    ) -> tp.Collection[sdk_models.Node]:
        actuals = [actual for target, actual in derivative_pairs]
        targets = [target for target, actual in derivative_pairs]

        # Status actualization
        if any(actual is None or actual.status != "ACTIVE" for actual in actuals):
            instance.status = "IN_PROGRESS"
        else:
            instance.status = "ACTIVE"

        # Return the same list of derivatives since
        # we aren't going to update them.
        return targets
```

The first method `create_instance_derivatives` is called when the instance is created. It creates infrastructure for the instance or a list of derivatives for the instance if more formal. The second method `actualize_outdated_instance_derivatives` is called when the instance is outdated. In our case it's called when something is changed in nodes. For example, a node changes its status or get IP address or something else. We need this method to update the instance status based on the nodes status.

And that's it! You have your own builder based on the universal builder. We can override more methods to provide custom logic but for the example we will stop here. See [Universal Builder](universal_builder.md) for more details.
