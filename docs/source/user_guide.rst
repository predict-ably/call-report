.. _user_guide:

==========
User Guide
==========

Task-oriented guides to working with ``call-report``. Each page takes one
capability and works through it end to end, with runnable examples against
real published data. If you are installing for the first time, start with
:ref:`getting_started` instead.

.. toctree::
   :maxdepth: 1
   :hidden:

   user_guide/schema_and_metadata
   user_guide/reshaping

.. grid:: 1 2 2 2
    :gutter: 3

    .. grid-item-card:: Schemas and Schedule Metadata
        :text-align: left
        :link: user_guide/schema_and_metadata
        :link-type: doc

        Inspect a schedule's fields, snapshot them at a quarter, and
        compare what a release shipped against what the package expects.

    .. grid-item-card:: Reshaping Across Schedules
        :text-align: left
        :link: user_guide/reshaping
        :link-type: doc

        Stack every schedule into one wide or long frame, and convert
        between the two shapes.
