from dolfin import *
import logging
import sys
import gc
import numpy as np
from petsc4py import PETSc


class EllipticPDE:
    """
    Class representing an elliptic partial differential equation.
    """

    def __init__(self, N):
        """
        Initialize the elliptic PDE problem.

        Parameters:
        N (int): Number of mesh divisions in each direction.
        """
        sh = logging.StreamHandler(sys.stdout)
        logging.basicConfig(level=logging.DEBUG, handlers=[sh])
        set_log_active(True)
        set_log_level(LogLevel.DEBUG)

        # Create FEniCS mesh and define function space
        mesh = UnitSquareMesh(N, N)
        V = FunctionSpace(mesh, "Lagrange", 1)
        self.V = V

    def setupProblem(self, difftype, y, quad_degree=0, varcoeffs=None, advection=False):
        """
        Set up the variational problem for the PDE.

        Parameters:
        difftype (str): Type of diffusion field (only 'cookie' implemented).
        y (array): Array of coefficients for the diffusion field.
        quad_degree (int, optional): Quadrature degree for integration (default is 0).
        varcoeffs (array, optional): Array of variable coefficients (default is None).
        advection (bool, optional): Whether to include advection term (default is False).
        """
        V = self.V

        # Define boundary condition
        u0 = Constant(0.0)

        # Homogeneous Dirichlet BC for unit square domain
        def boundary(x):
            return x[0] < DOLFIN_EPS or x[0] > 1.0 - DOLFIN_EPS or x[1] < DOLFIN_EPS or x[1] > 1.0 - DOLFIN_EPS

        bc = DirichletBC(V, u0, boundary)

        # Define variational problem
        u = TrialFunction(V)
        v = TestFunction(V)

        # Forcing on subdomain F defined by f_indicator
        f_indicator = Expression(
            "abs(x[0] - 0.5) < 0.1 and abs(x[1] -0.5) < 0.1", degree=quad_degree)
        f = Constant(100.0) * f_indicator

        # Diffusion field defined via indicators on appropriate subdomains
        if difftype == 'cookie':
            if varcoeffs is None:
                a0 = 1.0
            else:
                a0 = varcoeffs[0]

            # Cookie centres
            cxlist = [0.2, 0.5, 0.8, 0.2, 0.8, 0.2, 0.5, 0.8]
            cylist = [0.2, 0.2, 0.2, 0.5, 0.5, 0.8, 0.8, 0.8]

            # Cookie radius
            r = 0.13
            diff = a0
            for ii in range(8):
                indicator_ii = Expression("(sqrt(pow(x[0]-cx,2) + pow(x[1]-cy,2)) < r)",
                                          cx=cxlist[ii], cy=cylist[ii], r=r, degree=quad_degree)
                diff = diff + indicator_ii * Constant(1.0-a0 + y[ii])
        else:
            error("Unknown diffusion field")

        # Diffusion bilinear form
        a = inner(diff*grad(u), grad(v))*dx

        # Optional: additional advection term
        if advection is True:
            w = Expression(("4*(x[1]-0.5)*(1-4*pow(x[0]-0.5,2))",
                           "-4*(x[0]-0.5)*(1-4*pow(x[1]-0.5,2))"), degree=4)
            a = a + inner(w, grad(u)) * v * dx

        # Define linear form on RHS
        L = f*v*dx

        # Bilinear form for mass matrix
        m = inner(u, v)*dx

        # Generic function in approx space V
        u = Function(V)

        # Write to python object
        self.diffproject = project(diff, V)
        self.fproject = project(f, V)
        self.f_indicator = f_indicator
        self.a = a
        self.L = L
        self.bc = bc
        self.m = m
        self.u = u
        self.y = y

    def solve(self, pctype, tol):
        """
        Solve the elliptic PDE problem using PETSc.

        Parameters:
        pctype (str): Type of preconditioner to use ('ILU', 'JACOBI', or 'none').
        tol (float or str): Tolerance for the solver. If 'LU', use direct solver.

        Returns:
        numpy.ndarray: Solution vector.
        """
        # Assemble FEniCS matrices
        A = assemble(self.a)
        b = assemble(self.L)
        self.bc.apply(A)
        self.bc.apply(b)

        print("Solving for y=" + str(self.y))

        # Convert from fenics to petsc
        A_petsc = as_backend_type(A).mat()
        b_petsc = as_backend_type(b).vec()
        u_petsc = as_backend_type(self.u.vector()).vec()

        # Construct solver
        ksp = PETSc.KSP().create()
        pc = PETSc.PC().create()

        if tol == "LU":
            # IF tol is string LU use direct solver
            # Direct solver is full LU preconditioner
            pc.setType(PETSc.PC.Type.LU)  # Set the preconditioner type
            ksp.setType(PETSc.KSP.Type.PREONLY)  # Choose a solver type
            pc.setOperators(A_petsc)  # Attach the matrix to the preconditioner
        else:
            # Use GMRES to approximate solution of linear system
            ksp.setType(PETSc.KSP.Type.GMRES)  # Choose a solver type
            ksp.setTolerances(atol=tol)  # Set tolerance
        # Set up solver operator
        ksp.setOperators(A_petsc)  # Set the operator
        ksp.setFromOptions()  # Set PETSc options for the solver

        # Preconditioning
        if pctype == "ILU":
            pc.setType(PETSc.PC.Type.ILU)  # Set the preconditioner type
        elif pctype == "JACOBI":
            pc.setType(PETSc.PC.Type.JACOBI)  # Set the preconditioner type
        else:
            pass
        pc.setOperators(A_petsc)  # Attach the matrix to the preconditioner
        # Set the preconditioner (if desired)
        if pctype != "none" or tol == "LU":
            ksp.setPC(pc)

        # Solve the linear system
        ksp.solve(b_petsc, u_petsc)  # Solve A*x = b for x
        iternum = ksp.getIterationNumber()
        print("Solved in: "+str(iternum)+" iterations")

        # Write vector to FEniCS vector
        self.u.vector()[:] = u_petsc[:]

        # Destroy petsc objects
        A_petsc.destroy()
        b_petsc.destroy()
        u_petsc.destroy()
        ksp.destroy()
        pc.destroy()

        # Return dof as vector
        return self.u.vector().get_local()

    def projectref(self, n_ref):
        """
        Interpolate the solution to a finer mesh.

        Parameters:
        n_ref (int): Number of mesh divisions in each direction for the finer mesh.

        Returns:
        numpy.ndarray: Interpolated solution vector on the finer mesh.
        """
        # Define the reference mesh
        N = n_ref
        mesh = UnitSquareMesh(N, N)
        V = FunctionSpace(mesh, "Lagrange", 1)
        # Interpolate on mesh
        u_int = interpolate(self.u, V)
        # Return values at nodes
        return u_int.vector().get_local()

    def computebenchmarkqoi(self):
        """
        Compute the benchmark quantity of interest.

        Returns:
        float: Integral of the solution over the region F.
        """
        integral = assemble(self.f_indicator*self.u*dx)
        return integral

    def computenorm(self, x, normtype):
        """
        Compute the norm of the solution.

        Parameters:
        x (array): Solution vector.
        normtype (str): Type of norm to compute.

        Returns:
        float: Computed norm of the solution.
        """
        u = Function(self.V)
        u.vector().set_local(x[:])
        return norm(u, normtype)

    def writeSln(self, filename):
        """
        Write the solution to a Paraview file.

        Parameters:
        filename (str): Base name for the output file.
        """
        fileout = File(filename + ".pvd")
        fileout << self.u
        fileout << self.diffproject
        fileout << self.fproject


    def solveTime(self, tol, finalTime):
        """
        Solves the time-dependent problem using PETSc's TS solver.

        Args:
            tol (float): The relative tolerance for the solver.
            finalTime (float): The final time up to which the solution is computed.

        Returns:
            numpy.ndarray: The local part of the solution vector.
        """
        # Compute solution
        A = assemble(self.a)
        b = assemble(self.L)
        M = assemble(self.m)
        self.bc.apply(A)
        self.bc.apply(b)
        self.bc.apply(M)

        print("Solving for y=" + str(self.y))

        A_petsc = as_backend_type(A).mat()
        M_petsc = as_backend_type(M).mat()
        b_petsc = as_backend_type(b).vec()
        u_petsc = as_backend_type(self.u.vector()).vec()

        f = u_petsc.copy()
        fm = u_petsc.copy()

        ts = PETSc.TS().create()

        def rhs_function(ts, t, u, F, A, b):
            A.mult(u, F)
            F.axpby(1.0, -1.0, b)
            return

        def rhs_function_specific(ts, t, u, F):
            rhs_function(ts, t, u, F, A_petsc, b_petsc)

        ts.setRHSFunction(rhs_function_specific, f)

        def mass_matrix_multiply(ts, t, u, udot, F, M):
            M.mult(udot, F)  # F = M * u

        def mass_matrix_multiply_specific(ts, t, u, udot, F):
            mass_matrix_multiply(ts, t, u, udot, F, M_petsc)

        ts.setIFunction(mass_matrix_multiply_specific, fm)

        vtkfile = File("output.pvd")  # Output file name (with .pvd extension)

        def monitor(ts, step, t, u):
            print(f"Time Step {step}: Time = {t}")
            self.u.vector()[:] = u[:]
            self.u.rename("u", "label")
            vtkfile << (self.u, t)  # Write function u to VTK file

        # Set the monitor function for the TS solver
        ts.setMonitor(monitor)

        # Create a PETSc options object
        opts = PETSc.Options()

        # Set the adaptive basis type (e.g., default is "basic")
        opts.setValue('-ts_adapt_type', 'basic')
        # Other adaptive basis options can be set here

        # Set solver options for adaptive time-stepping

        ts.setProblemType(ts.ProblemType.LINEAR)
        # ts.setEquationType(ts.EquationType.IMPLICIT)
        ts.setType(ts.Type.BEULER)

        ts.setSolution(u_petsc)
        ts.setMaxTime(float(finalTime))

        ts.setTolerances(rtol=tol)
        ts.setTimeStep(1e-8)
        ts.setFromOptions()

        # ts.setTimeStep(1e-8)
        ts.solve(u_petsc)

        PETScOptions.clear()
        ts.reset()
        ts.destroy()

        self.u.vector()[:] = u_petsc[:]

        A_petsc.destroy()
        M_petsc.destroy()
        b_petsc.destroy()
        u_petsc.destroy()
        f.destroy()
        fm.destroy()

        gc.collect()
        # PETSc.Log.view()

        return self.u.vector().get_local()


    def solveTimeSimple(self, tol, finalTime):
        """
        Solves the time-dependent problem using a simple time-stepping approach.

        Args:
            tol (float): The relative tolerance for the solver.
            finalTime (float): The final time up to which the solution is computed.

        Returns:
            numpy.ndarray: The local part of the solution vector.
        """
        # PETSc.Options().setValue('-log_view', '')
        # PETSc.Options().setValue('-malloc_view', '')
        # PETSc.Log.begin()

        # Compute solution
        A = assemble(self.a)
        b = assemble(self.L)
        M = assemble(self.m)
        self.bc.apply(A)
        self.bc.apply(b)
        self.bc.apply(M)

        print("Solving for y=" + str(self.y))

        A_petsc = as_backend_type(A).mat()
        M_petsc = as_backend_type(M).mat()
        b_petsc = as_backend_type(b).vec()
        u_petsc = as_backend_type(self.u.vector()).vec()
        A = None
        b = None
        M = None
        gc.collect()

        # Output file name (with .pvd extension)
        vtkfile = File("output" + str(tol) + ".pvd")

        def monitor(ts, step, t, u, u2, enorm):
            print(f"Time = {t:.4g},\t dt = {ts:.4g},\t E = {enorm:.4g},\t acc = {acc:.4g}")
            # self.u.vector()[:] = u[:]
            # self.u.rename("u", "label")
            # vtkfile << (self.u, t)  # Write function u to VTK file

        ksp = PETSc.KSP().create()

        t = 0.0
        dt = 1e-5
        dtold = 1e9

        lhs = A_petsc.copy()
        lhs_old = A_petsc.copy()
        rhs = u_petsc.copy()
        rhs_temp = u_petsc.copy()
        abf2 = u_petsc.copy()
        u_petsc_m1 = u_petsc.copy()
        e_petsc = u_petsc.copy()
        e_temp = u_petsc.copy()

        ii = 0
        while t < finalTime:
            if t + dt > finalTime:
                dt = finalTime - t

            lhs.zeroEntries()
            lhs_old.zeroEntries()
            rhs.zeroEntries()

            # lhs_old = M_petsc + 0.5*dt*A_petsc
            lhs.axpy(0.5 * dt, A_petsc)
            lhs.axpy(1.0, M_petsc)

            # rhs = (M_petsc - 0.5*dt*A_petsc) * u_petsc + dt*b_petsc
            M_petsc.mult(u_petsc, rhs)
            A_petsc.mult(u_petsc, rhs_temp)
            rhs.axpy(-0.5 * dt, rhs_temp)
            rhs.axpy(dt, b_petsc)

            ksp.setOperators(lhs)  # Set the operator

            # abf2 = u_petsc + dt * (-1.5 * A_petsc * u_petsc + 0.5 * A_petsc * u_petsc_m1)
            A_petsc.mult(u_petsc, abf2)
            A_petsc.mult(u_petsc_m1, rhs_temp)
            abf2.axpby(0.5 * (dt / dtold), -(1 + 0.5 * dt / dtold), rhs_temp)
            abf2.axpby(1.0, dt, u_petsc)
            abf2.axpy(dt, b_petsc)

            u_petsc_m1.axpby(1.0, 0.0, u_petsc)

            ksp.solve(rhs, u_petsc)

            # print(u_petsc[:])

            e_petsc.zeroEntries()
            e_petsc.axpby(1.0, 0.0, abf2)
            e_petsc.axpy(-1.0, u_petsc)
            M_petsc.mult(e_petsc, e_temp)
            enorm = (dt / (3 * (1 + dtold / dt))) * np.sqrt(e_petsc.dot(e_temp))
            ii += 1
            t += dt
            acc = float(np.power(tol / enorm, 2 / 3))
            if acc > 10:
                acc = 10  # Limit to exponential growth
            monitor(dt, ii, t, u_petsc, abf2, enorm)
            dtold = dt
            if ii != 1:
                dt = dt * acc  # Don't adapt first step

            ksp.reset()

        ksp.destroy()

        self.u.vector()[:] = u_petsc[:]

        PETScOptions.clear()

        A_petsc.destroy()
        M_petsc.destroy()
        b_petsc.destroy()
        u_petsc.destroy()

        lhs.destroy()
        lhs_old.destroy()
        rhs.destroy()