{{ fullname | escape | underline}}

.. currentmodule:: {{ module }}

.. autoclass:: {{ objname }}
   :members:
   :show-inheritance:
{%- if fullname in inherited_member_classes %}
   :inherited-members:
{%- endif %}
